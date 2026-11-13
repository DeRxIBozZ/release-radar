"""Background job that turns TMDB data into rows in the `releases` table.

A "scope" is one (region, source) pair, e.g. ("DE", "theatrical") or
("US", "p:8") for Netflix in the US. The job syncs the union of all scopes
that any user tracks, so the feed itself is a plain database query.
"""
import asyncio
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import auth, db, tmdb

log = logging.getLogger("radar.sync")

REGIONS = ["DE", "US"]
LANG = "en-US"
PAST_DAYS = int(os.environ.get("PAST_DAYS", "90"))
FUTURE_DAYS = int(os.environ.get("FUTURE_DAYS", "365"))
INTERVAL = int(float(os.environ.get("SYNC_HOURS", "12")) * 3600)
MONETIZATION = "flatrate|free|ads"
# Talk, News and Soap air daily and would drown everything else.
TV_EXCLUDED_GENRES = "10767,10763,10766"

# TMDB only attaches a streaming provider to a title once it is available
# there. For shows that have not started yet we additionally look at the
# "network" a series was ordered by. Keys are provider ids, values network ids.
PROVIDER_NETWORKS = {
    8: [213],        # Netflix
    337: [2739],     # Disney+
    9: [1024],       # Amazon Prime Video
    119: [1024],
    350: [2552],     # Apple TV+
    15: [453],       # Hulu
    1899: [3186],    # Max / HBO Max
    384: [3186],
    531: [4330],     # Paramount+
    386: [3353],     # Peacock
}

status = {"running": False, "progress": "", "last_finished": None, "last_error": None, "errors": []}
_trigger = threading.Event()


def today() -> date:
    try:
        return datetime.now(ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))).date()
    except Exception:
        return date.today()


def trigger():
    _trigger.set()


# --- which scopes are needed --------------------------------------------------

def scopes_for_prefs(prefs: dict, provider_regions: dict[int, list[str]]) -> set[tuple[str, str]]:
    out = set()
    for region in prefs.get("regions") or []:
        if region not in REGIONS:
            continue
        if prefs.get("theatrical"):
            out.add((region, "theatrical"))
        if prefs.get("digital"):
            out.add((region, "digital"))
        for pid in prefs.get("providers") or []:
            # Before the provider list is known, assume the provider exists everywhere.
            if not provider_regions or region in provider_regions.get(pid, []):
                out.add((region, f"p:{pid}"))
    return out


def provider_regions() -> dict[int, list[str]]:
    with db.tx() as conn:
        return {r["id"]: json.loads(r["regions"]) for r in conn.execute("SELECT id, regions FROM providers")}


def needed_scopes() -> set[tuple[str, str]]:
    regions = provider_regions()
    with db.tx() as conn:
        users = conn.execute("SELECT prefs FROM users").fetchall()
    out = set()
    for u in users:
        out |= scopes_for_prefs(auth.user_prefs(u), regions)
    return out


def synced_scopes() -> set[tuple[str, str]]:
    return {tuple(s) for s in json.loads(db.get_meta("synced_scopes", "[]"))}


# --- reference data -----------------------------------------------------------

async def sync_reference(c: tmdb.Client):
    providers: dict[int, dict] = {}
    for region in REGIONS:
        for kind in ("movie", "tv"):
            data = await c.get(f"/watch/providers/{kind}", {"watch_region": region, "language": LANG}, ttl=86400)
            for p in (data or {}).get("results", []):
                entry = providers.setdefault(
                    p["provider_id"],
                    {"name": p.get("provider_name") or "?", "logo": p.get("logo_path"), "regions": set(), "prio": 999},
                )
                entry["regions"].add(region)
                prio = (p.get("display_priorities") or {}).get(region, p.get("display_priority", 999))
                entry["prio"] = min(entry["prio"], prio if isinstance(prio, int) else 999)
    genres: dict[int, str] = {}
    for kind in ("movie", "tv"):
        data = await c.get(f"/genre/{kind}/list", {"language": LANG}, ttl=86400)
        for g in (data or {}).get("genres", []):
            genres[g["id"]] = g["name"]
    with db.tx() as conn:
        if providers:
            conn.execute("DELETE FROM providers")
            conn.executemany(
                "INSERT INTO providers(id, name, logo_path, regions, priority) VALUES(?,?,?,?,?)",
                [(pid, p["name"], p["logo"], json.dumps(sorted(p["regions"])), p["prio"]) for pid, p in providers.items()],
            )
        if genres:
            conn.execute("DELETE FROM genres")
            conn.executemany("INSERT INTO genres(id, name) VALUES(?,?)", list(genres.items()))


# --- helpers ------------------------------------------------------------------

def chunks(start: date, end: date, days: int = 31):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        yield cur.isoformat(), nxt.isoformat()
        cur = nxt + timedelta(days=1)


async def discover_ids(c: tmdb.Client, kind: str, params: dict, max_pages: int) -> set[int]:
    params = {"language": LANG, "include_adult": "false", "sort_by": "popularity.desc", **params}
    first = await c.get(f"/discover/{kind}", {**params, "page": 1})
    if not first:
        return set()
    pages = min(int(first.get("total_pages") or 1), max_pages)
    rest = await asyncio.gather(*[c.get(f"/discover/{kind}", {**params, "page": p}) for p in range(2, pages + 1)])
    ids = set()
    for page in [first, *rest]:
        for item in (page or {}).get("results", []):
            ids.add(item["id"])
    return ids


def title_row(kind: str, d: dict):
    return (
        kind,
        d["id"],
        d.get("title") or d.get("name") or d.get("original_title") or d.get("original_name") or "Untitled",
        d.get("overview") or "",
        d.get("poster_path"),
        json.dumps([g["id"] for g in d.get("genres") or []]),
        d.get("popularity") or 0,
        d.get("vote_average") or 0,
        d.get("vote_count") or 0,
    )


def regional_dates(details: dict, region: str) -> dict[int, list[str]]:
    """Release dates of a movie in one region, keyed by TMDB release type.

    Types: 1 premiere, 2 limited theatrical, 3 theatrical, 4 digital, 5 physical, 6 TV.
    """
    out: dict[int, list[str]] = {}
    for entry in ((details.get("release_dates") or {}).get("results") or []):
        if entry.get("iso_3166_1") != region:
            continue
        for rd in entry.get("release_dates") or []:
            d = (rd.get("release_date") or "")[:10]
            if d:
                out.setdefault(rd.get("type"), []).append(d)
    for v in out.values():
        v.sort()
    return out


RERELEASE_AFTER = timedelta(days=365)


# Release types that count as "the same kind" when looking for an earlier release.
# Festival screenings (1), physical media (5) and TV (6) never make a later date a re-release.
SAME_KIND = {2: (2, 3), 3: (2, 3), 4: (4,)}


def first_of_kind(details: dict, region: str, rtype: int) -> str | None:
    """Earliest release in a region of the same kind as rtype (cinema or digital)."""
    dates = regional_dates(details, region)
    found = [d for t in SAME_KIND.get(rtype, ()) for d in dates.get(t, [])]
    return min(found) if found else None


def release_notes(details: dict, region: str, rtype: int) -> dict[str, str]:
    """TMDB's free-text note per release date, e.g. 'Avengers Endgame: Encore'."""
    out: dict[str, str] = {}
    for entry in ((details.get("release_dates") or {}).get("results") or []):
        if entry.get("iso_3166_1") != region:
            continue
        for rd in entry.get("release_dates") or []:
            d = (rd.get("release_date") or "")[:10]
            if d and rd.get("type") == rtype and not out.get(d):
                out[d] = (rd.get("note") or "").strip()[:120]
    return out


OLD_FILM_AFTER = timedelta(days=5 * 365)


def is_rerelease(details: dict, region: str, rtype: int, when: str) -> int:
    """A release counts as a re-release when either
    - the film already had a release of the same kind (cinema or digital) in
      this region more than a year earlier, or
    - the film's original release date is more than five years earlier
      (an old film arriving now, even if TMDB lacks its earlier regional dates).
      Five years, because foreign films often reach a country two to four years late.
    """
    d = date.fromisoformat(when)
    first = first_of_kind(details, region, rtype)
    if first and d - date.fromisoformat(first) > RERELEASE_AFTER:
        return 1
    original = details.get("release_date") or ""
    try:
        if original and d - date.fromisoformat(original) > OLD_FILM_AFTER:
            return 1
    except ValueError:
        pass
    return 0


async def movie_details(c: tmdb.Client, movie_id: int):
    return await c.get(f"/movie/{movie_id}", {"language": LANG, "append_to_response": "release_dates"})


# --- scope builders -----------------------------------------------------------
# Each returns (title rows, release rows). Release rows are
# (media_type, tmdb_id, region, source, date, season, ep_from, ep_to, ep_total, is_premiere,
#  is_rerelease, note)

async def scope_release_type(c, region: str, source: str, start: date, end: date):
    rtype = 3 if source == "theatrical" else 4
    ids: set[int] = set()
    results = await asyncio.gather(*[
        discover_ids(c, "movie", {
            "region": region, "with_release_type": rtype,
            "release_date.gte": a, "release_date.lte": b,
        }, max_pages=5)
        for a, b in chunks(start, end)
    ])
    for r in results:
        ids |= r
    s, e = start.isoformat(), end.isoformat()
    titles, rows = [], []
    for d in await asyncio.gather(*[movie_details(c, i) for i in ids]):
        if not d:
            continue
        dates = sorted({x for x in regional_dates(d, region).get(rtype, []) if s <= x <= e})
        if not dates:
            continue
        titles.append(title_row("movie", d))
        notes = release_notes(d, region, rtype)
        for x in dates:
            rows.append(("movie", d["id"], region, source, x, 0, 0, 0, 0, 0,
                         is_rerelease(d, region, rtype, x), notes.get(x, "")))
    return titles, rows


async def scope_provider_movies(c, region: str, pid: int, start: date, end: date):
    # TMDB does not know when a title was added to a service, so the date is
    # the regional digital release, or the regional premiere for titles that
    # skipped cinemas. Older catalogue additions are therefore not detected.
    ids = await discover_ids(c, "movie", {
        "watch_region": region, "with_watch_providers": pid,
        "with_watch_monetization_types": MONETIZATION,
        "primary_release_date.gte": (start - timedelta(days=365)).isoformat(),
        "primary_release_date.lte": end.isoformat(),
    }, max_pages=15)
    s, e = start.isoformat(), end.isoformat()
    titles, rows = [], []
    for d in await asyncio.gather(*[movie_details(c, i) for i in ids]):
        if not d:
            continue
        dates = regional_dates(d, region)
        if dates.get(4):
            when = dates[4][0]
        elif dates.get(3) or dates.get(2):
            continue  # cinema release with unknown streaming date
        elif dates:
            when = min(v[0] for v in dates.values())
        else:
            when = d.get("release_date") or ""
        if not (s <= when <= e):
            continue
        titles.append(title_row("movie", d))
        # `when` is the first digital date, so by definition never a re-release.
        rows.append(("movie", d["id"], region, f"p:{pid}", when, 0, 0, 0, 0, 0, 0, ""))
    return titles, rows


async def tv_events(c, tv_id: int, s: str, e: str):
    d = await c.get(f"/tv/{tv_id}", {"language": LANG})
    if not d:
        return None, []
    wanted = set()
    for key in ("last_episode_to_air", "next_episode_to_air"):
        n = (d.get(key) or {}).get("season_number")
        if n:
            wanted.add(n)
    season_dates = {}
    for season in d.get("seasons") or []:
        n, ad = season.get("season_number"), season.get("air_date")
        if n and ad:
            season_dates[n] = ad
            if s <= ad <= e:
                wanted.add(n)
    events = []
    for n in sorted(wanted):
        sd = await c.get(f"/tv/{tv_id}/season/{n}", {"language": LANG})
        episodes = (sd or {}).get("episodes") or []
        total = len(episodes)
        by_date: dict[str, list[int]] = {}
        dated = 0
        for ep in episodes:
            ad, num = ep.get("air_date"), ep.get("episode_number")
            if not ad or not num:
                continue
            dated += 1
            if s <= ad <= e:
                by_date.setdefault(ad, []).append(num)
        for ad, nums in by_date.items():
            events.append((ad, n, min(nums), max(nums), total, 1 if min(nums) == 1 else 0))
        if not dated and s <= season_dates.get(n, "") <= e:
            events.append((season_dates[n], n, 0, 0, total, 1))
    return d, events


async def scope_provider_tv(c, region: str, pid: int, start: date, end: date):
    now = today()
    windows = [(start.isoformat(), now.isoformat()), ((now + timedelta(days=1)).isoformat(), end.isoformat())]
    queries = []
    for a, b in windows:
        base = {"air_date.gte": a, "air_date.lte": b, "without_genres": TV_EXCLUDED_GENRES}
        queries.append(discover_ids(c, "tv", {
            **base, "watch_region": region, "with_watch_providers": pid,
            "with_watch_monetization_types": MONETIZATION,
        }, max_pages=8))
        for network in PROVIDER_NETWORKS.get(pid, []):
            queries.append(discover_ids(c, "tv", {**base, "with_networks": network}, max_pages=8))
    ids: set[int] = set()
    for r in await asyncio.gather(*queries):
        ids |= r
    s, e = start.isoformat(), end.isoformat()
    titles, rows = [], []
    for d, events in await asyncio.gather(*[tv_events(c, i, s, e) for i in ids]):
        if not d or not events:
            continue
        titles.append(title_row("tv", d))
        for when, season, ep_from, ep_to, total, premiere in events:
            rows.append(("tv", d["id"], region, f"p:{pid}", when, season, ep_from, ep_to, total, premiere, 0, ""))
    return titles, rows


async def build_scope(c, region: str, source: str, start: date, end: date):
    if source in ("theatrical", "digital"):
        return await scope_release_type(c, region, source, start, end)
    pid = int(source[2:])
    (t1, r1), (t2, r2) = await asyncio.gather(
        scope_provider_movies(c, region, pid, start, end),
        scope_provider_tv(c, region, pid, start, end),
    )
    return t1 + t2, r1 + r2


def store_scope(region: str, source: str, titles: list, rows: list):
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO titles(media_type, tmdb_id, title, overview, poster_path, genres, popularity, vote_average, vote_count) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(media_type, tmdb_id) DO UPDATE SET "
            "title=excluded.title, overview=excluded.overview, poster_path=excluded.poster_path, "
            "genres=excluded.genres, popularity=excluded.popularity, vote_average=excluded.vote_average, "
            "vote_count=excluded.vote_count",
            titles,
        )
        conn.execute("DELETE FROM releases WHERE region=? AND source=?", (region, source))
        conn.executemany(
            "INSERT OR IGNORE INTO releases(media_type, tmdb_id, region, source, date, season, ep_from, "
            "ep_to, ep_total, is_premiere, is_rerelease, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


# --- orchestration ------------------------------------------------------------

async def run_sync():
    c = tmdb.Client()
    errors = []
    try:
        status["progress"] = "Loading providers and genres"
        await sync_reference(c)
        scopes = sorted(needed_scopes())
        start = today() - timedelta(days=PAST_DAYS)
        end = today() + timedelta(days=FUTURE_DAYS)
        done = []
        for i, (region, source) in enumerate(scopes, 1):
            status["progress"] = f"Syncing {source} in {region} ({i}/{len(scopes)})"
            try:
                titles, rows = await build_scope(c, region, source, start, end)
                store_scope(region, source, titles, rows)
                done.append((region, source))
                log.info("synced %s/%s: %d releases", region, source, len(rows))
            except tmdb.TMDBError as e:
                if "401" in str(e):
                    raise
                log.error("scope %s/%s failed: %s", region, source, e)
                errors.append(f"{region}/{source}: {e}")
        # Drop data nobody tracks any more and anything that fell out of the window.
        with db.tx() as conn:
            keep = set(scopes)
            for r in conn.execute("SELECT DISTINCT region, source FROM releases").fetchall():
                if (r["region"], r["source"]) not in keep:
                    conn.execute("DELETE FROM releases WHERE region=? AND source=?", (r["region"], r["source"]))
            conn.execute("DELETE FROM releases WHERE date < ?", (start.isoformat(),))
            conn.execute(
                "DELETE FROM titles WHERE NOT EXISTS (SELECT 1 FROM releases r "
                "WHERE r.media_type = titles.media_type AND r.tmdb_id = titles.tmdb_id)"
            )
        db.set_meta("synced_scopes", json.dumps(sorted(set(done) | (synced_scopes() & set(scopes)))))
        tmdb.purge_cache()
        log.info("sync finished: %d scopes, %d TMDB requests", len(scopes), c.requests)
    finally:
        await c.close()
    return errors


def _loop():
    while True:
        _trigger.clear()
        if not tmdb.token():
            status.update(last_error="TMDB_TOKEN is not set. Add it to the compose file and restart.", progress="")
            log.error(status["last_error"])
        else:
            status.update(running=True, progress="Starting")
            try:
                errors = asyncio.run(run_sync())
                status.update(last_error=None, errors=errors, last_finished=int(time.time()))
                db.set_meta("last_sync", str(int(time.time())))
            except Exception as e:  # keep the loop alive whatever happens
                log.exception("sync failed")
                status.update(last_error=str(e) or type(e).__name__)
            finally:
                status.update(running=False, progress="")
        _trigger.wait(INTERVAL if not status["last_error"] else min(INTERVAL, 900))


def start():
    last = db.get_meta("last_sync")
    status["last_finished"] = int(last) if last else None
    threading.Thread(target=_loop, name="sync", daemon=True).start()
