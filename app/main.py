import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs may contain the API key

STATIC = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    auth.ensure_admin()
    sync.start()
    yield


app = FastAPI(title="Release Radar", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # A custom header cannot be sent cross-site without a CORS preflight,
    # which this app never approves. Together with SameSite cookies this
    # blocks CSRF on every state-changing request.
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD"):
        if request.headers.get("x-requested-with") != "release-radar":
            return JSONResponse({"detail": "Bad request"}, status_code=400)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def public_user(row):
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]), "created_at": row["created_at"]}


def clean_prefs(body: dict) -> dict:
    regions = [r for r in sync.REGIONS if r in (body.get("regions") or [])]
    providers = []
    for p in body.get("providers") or []:
        if isinstance(p, int) and not isinstance(p, bool) and p not in providers:
            providers.append(p)
    return {
        "regions": regions,
        "theatrical": bool(body.get("theatrical")),
        "digital": bool(body.get("digital")),
        "providers": providers[:60],
        "tv_mode": "episodes" if body.get("tv_mode") == "episodes" else "premieres",
    }


# --- session ------------------------------------------------------------------

@app.post("/api/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    ip = request.client.host if request.client else "?"
    auth.check_throttle(ip)
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    # Always run a hash so response time does not reveal whether the user exists.
    ok = auth.verify_password(password, row["password_hash"] if row else "scrypt$00$00")
    if not row or not ok:
        auth.record_failure(ip)
        raise HTTPException(401, "Wrong username or password")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        auth.COOKIE, auth.create_session(row["id"]), max_age=auth.SESSION_TTL,
        httponly=True, samesite="lax", secure=secure, path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE)
    if token:
        auth.destroy_session(token)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(auth.current_user)):
    try:
        filters = json.loads(user["filters"] or "{}")
    except ValueError:
        filters = {}
    return {"user": public_user(user), "prefs": auth.user_prefs(user), "filters": filters}


@app.put("/api/prefs")
def save_prefs(body: dict = Body(...), user=Depends(auth.current_user)):
    prefs = clean_prefs(body)
    with db.tx() as conn:
        conn.execute("UPDATE users SET prefs=? WHERE id=?", (json.dumps(prefs), user["id"]))
    syncing = False
    if not sync.needed_scopes() <= sync.synced_scopes():
        sync.trigger()
        syncing = True
    return {"prefs": prefs, "syncing": syncing}


@app.put("/api/filters")
def save_filters(body: dict = Body(...), user=Depends(auth.current_user)):
    raw = json.dumps(body)
    if len(raw) > 4000:
        raise HTTPException(400, "Too large")
    with db.tx() as conn:
        conn.execute("UPDATE users SET filters=? WHERE id=?", (raw, user["id"]))
    return {"ok": True}


@app.post("/api/password")
def change_password(body: dict = Body(...), user=Depends(auth.current_user)):
    if not auth.verify_password(str(body.get("current") or ""), user["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    auth.validate_new_password(body.get("new"))
    with db.tx() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body["new"]), user["id"]))
    return {"ok": True}


# --- reference data and feed --------------------------------------------------

def sync_status():
    s = sync.status
    return {
        "running": s["running"], "progress": s["progress"], "last_finished": s["last_finished"],
        "error": s["last_error"], "warnings": s["errors"][:10],
    }


@app.get("/api/meta")
def meta(user=Depends(auth.current_user)):
    with db.tx() as conn:
        providers = [
            {"id": r["id"], "name": r["name"], "logo": r["logo_path"], "regions": json.loads(r["regions"])}
            for r in conn.execute("SELECT * FROM providers ORDER BY priority, name")
        ]
        genres = [{"id": r["id"], "name": r["name"]} for r in conn.execute("SELECT * FROM genres ORDER BY name")]
    return {"providers": providers, "genres": genres, "regions": sync.REGIONS, "status": sync_status()}


@app.get("/api/status")
def get_status(user=Depends(auth.current_user)):
    return sync_status()


def _ints(csv: str) -> set[int]:
    return {int(x) for x in csv.split(",") if x.strip().lstrip("-").isdigit()}


@app.get("/api/feed")
def feed(
    tab: str = "released", type: str = "all", genres: str = "", sources: str = "",
    region: str = "all", q: str = "", offset: int = 0, limit: int = 60,
    user=Depends(auth.current_user),
):
    prefs = auth.user_prefs(user)
    scopes = sync.scopes_for_prefs(prefs, sync.provider_regions())
    if region in sync.REGIONS:
        scopes = {s for s in scopes if s[0] == region}
    wanted_sources = {s for s in sources.split(",") if s}
    if wanted_sources:
        scopes = {s for s in scopes if s[1] in wanted_sources}
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if not scopes:
        return {"items": [], "total": 0, "next_offset": None}

    upcoming = tab == "upcoming"
    today = sync.today().isoformat()
    where = ["r.date > ?" if upcoming else "r.date <= ?"]
    args: list = [today]
    regions_in = sorted({s[0] for s in scopes})
    sources_in = sorted({s[1] for s in scopes})
    where.append(f"r.region IN ({','.join('?' * len(regions_in))})")
    where.append(f"r.source IN ({','.join('?' * len(sources_in))})")
    args += regions_in + sources_in
    if type in ("movie", "tv"):
        where.append("r.media_type = ?")
        args.append(type)
    if prefs.get("tv_mode") != "episodes":
        where.append("(r.media_type = 'movie' OR r.is_premiere = 1)")
    if q.strip():
        where.append("t.title LIKE ? ESCAPE '\\'")
        args.append("%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    order = "r.date ASC" if upcoming else "r.date DESC"
    sql = (
        "SELECT r.*, t.title, t.overview, t.poster_path, t.genres, t.popularity, t.vote_average "
        "FROM releases r JOIN titles t ON t.media_type = r.media_type AND t.tmdb_id = r.tmdb_id "
        f"WHERE {' AND '.join(where)} ORDER BY {order}, t.popularity DESC, t.title"
    )
    with db.tx() as conn:
        rows = conn.execute(sql, args).fetchall()

    genre_filter = _ints(genres)
    items: dict[tuple, dict] = {}
    for r in rows:
        if (r["region"], r["source"]) not in scopes:
            continue
        g = json.loads(r["genres"])
        if genre_filter and not genre_filter & set(g):
            continue
        key = (r["media_type"], r["tmdb_id"], r["date"], r["season"], r["ep_from"])
        item = items.get(key)
        if item is None:
            item = items[key] = {
                "media_type": r["media_type"], "tmdb_id": r["tmdb_id"], "title": r["title"],
                "overview": r["overview"], "poster": r["poster_path"], "genres": g,
                "vote": round(r["vote_average"], 1), "date": r["date"], "season": r["season"],
                "ep_from": r["ep_from"], "ep_to": r["ep_to"], "ep_total": r["ep_total"],
                "premiere": bool(r["is_premiere"]), "where": [],
            }
        item["where"].append({"region": r["region"], "source": r["source"]})
    ordered = list(items.values())
    page = ordered[offset:offset + limit]
    nxt = offset + limit if offset + limit < len(ordered) else None
    return {"items": page, "total": len(ordered), "next_offset": nxt, "today": today}


# --- admin --------------------------------------------------------------------

@app.get("/api/users")
def list_users(admin=Depends(auth.admin_user)):
    with db.tx() as conn:
        return [public_user(r) for r in conn.execute("SELECT * FROM users ORDER BY username")]


@app.post("/api/users")
def create_user(body: dict = Body(...), admin=Depends(auth.admin_user)):
    username = str(body.get("username") or "").strip()
    if not (1 <= len(username) <= 40):
        raise HTTPException(400, "Username must be 1 to 40 characters")
    auth.validate_new_password(body.get("password"))
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, is_admin, prefs) VALUES(?,?,?,?)",
                (username, auth.hash_password(body["password"]), 1 if body.get("is_admin") else 0,
                 json.dumps(auth.DEFAULT_PREFS)),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(400, "That username already exists")
    if not sync.needed_scopes() <= sync.synced_scopes():
        sync.trigger()
    return {"ok": True}


def _admin_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin=1").fetchone()["n"]


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, body: dict = Body(...), admin=Depends(auth.admin_user)):
    with db.tx() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        if "password" in body:
            auth.validate_new_password(body["password"])
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body["password"]), user_id))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        if "is_admin" in body:
            make_admin = 1 if body["is_admin"] else 0
            if not make_admin and target["is_admin"] and _admin_count(conn) <= 1:
                raise HTTPException(400, "There must be at least one admin")
            conn.execute("UPDATE users SET is_admin=? WHERE id=?", (make_admin, user_id))
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin=Depends(auth.admin_user)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You cannot delete your own account")
    with db.tx() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        if target["is_admin"] and _admin_count(conn) <= 1:
            raise HTTPException(400, "There must be at least one admin")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True}


@app.post("/api/sync")
def sync_now(admin=Depends(auth.admin_user)):
    sync.trigger()
    return {"ok": True}


# --- static -------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
