import asyncio
import json
import logging
import os
import time

import httpx

from . import db

log = logging.getLogger("radar.tmdb")

BASE = os.environ.get("TMDB_BASE_URL", "https://api.themoviedb.org/3").rstrip("/")
CACHE_TTL = int(float(os.environ.get("CACHE_HOURS", "10")) * 3600)


class TMDBError(Exception):
    pass


def token() -> str:
    return (os.environ.get("TMDB_TOKEN") or os.environ.get("TMDB_API_KEY") or "").strip()


class Client:
    """Async TMDB client with a persistent response cache.

    Accepts either the v4 "API Read Access Token" (sent as a Bearer header)
    or the short v3 API key (sent as the api_key query parameter).
    """

    def __init__(self, concurrency: int = 10):
        tok = token()
        if not tok:
            raise TMDBError("TMDB_TOKEN is not set")
        headers = {"Accept": "application/json"}
        self.auth_params = {}
        if tok.startswith("eyJ") or len(tok) > 64:
            headers["Authorization"] = f"Bearer {tok}"
        else:
            self.auth_params["api_key"] = tok
        self.http = httpx.AsyncClient(headers=headers, timeout=25)
        self.sem = asyncio.Semaphore(concurrency)
        self.inflight: dict[str, asyncio.Task] = {}
        self.requests = 0

    async def close(self):
        await self.http.aclose()

    async def get(self, path: str, params: dict | None = None, ttl: int | None = None):
        """GET a TMDB resource. Returns parsed JSON, or None for a 404."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        task = self.inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._get(key, path, params, CACHE_TTL if ttl is None else ttl))
            self.inflight[key] = task
        return await task

    async def _get(self, key, path, params, ttl):
        now = int(time.time())
        with db.tx() as conn:
            row = conn.execute("SELECT fetched_at, body FROM cache WHERE key=?", (key,)).fetchone()
        if row and now - row["fetched_at"] < ttl:
            return json.loads(row["body"])

        data = await self._fetch(path, params)
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO cache(key, fetched_at, body) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET fetched_at=excluded.fetched_at, body=excluded.body",
                (key, now, json.dumps(data, separators=(",", ":"))),
            )
        return data

    async def _fetch(self, path, params):
        url = BASE + path
        last = None
        for attempt in range(5):
            async with self.sem:
                self.requests += 1
                try:
                    r = await self.http.get(url, params={**params, **self.auth_params})
                except httpx.HTTPError as e:
                    last = f"{type(e).__name__}: {e}"
                    r = None
            if r is not None:
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                if r.status_code == 401:
                    raise TMDBError("TMDB rejected the token (401). Check TMDB_TOKEN.")
                last = f"HTTP {r.status_code}"
                if r.status_code == 429:
                    try:
                        await asyncio.sleep(min(float(r.headers.get("Retry-After", "2")), 30))
                    except ValueError:
                        await asyncio.sleep(2)
                    continue
                if r.status_code < 500:
                    raise TMDBError(f"TMDB {path}: {last} {r.text[:200]}")
            await asyncio.sleep(1.5 * (attempt + 1))
        raise TMDBError(f"TMDB {path} failed after retries: {last}")


def purge_cache(max_age: int = 3 * 24 * 3600):
    with db.tx() as conn:
        conn.execute("DELETE FROM cache WHERE fetched_at < ?", (int(time.time()) - max_age,))
