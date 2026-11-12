import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from fastapi import HTTPException, Request

from . import db

log = logging.getLogger("radar.auth")

COOKIE = "rr_session"
SESSION_TTL = 30 * 24 * 3600
MIN_PASSWORD = 8

DEFAULT_PREFS = {
    "regions": ["DE", "US"],
    "theatrical": True,
    "digital": False,
    "providers": [8, 337, 9],  # Netflix, Disney+, Prime Video
    "tv_mode": "premieres",    # 'premieres' | 'episodes'
}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?,?,?)",
            (_token_hash(token), user_id, now + SESSION_TTL),
        )
    return token


def destroy_session(token: str):
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def user_prefs(row) -> dict:
    prefs = dict(DEFAULT_PREFS)
    try:
        prefs.update(json.loads(row["prefs"] or "{}"))
    except ValueError:
        pass
    return prefs


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Not logged in")
    with db.tx() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash=? AND s.expires_at > ?",
            (_token_hash(token), int(time.time())),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Not logged in")
    return row


def admin_user(request: Request):
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    return user


def validate_new_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters")


def ensure_admin():
    """Create the first admin account from the environment if no users exist."""
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        username = os.environ.get("ADMIN_USER", "admin").strip() or "admin"
        password = os.environ.get("ADMIN_PASSWORD", "")
        generated = len(password) < MIN_PASSWORD
        if generated:
            password = secrets.token_urlsafe(12)
        conn.execute(
            "INSERT INTO users(username, password_hash, is_admin, prefs) VALUES(?,?,1,?)",
            (username, hash_password(password), json.dumps(DEFAULT_PREFS)),
        )
    if generated:
        log.warning(
            "ADMIN_PASSWORD missing or shorter than %d characters. "
            "Created admin '%s' with generated password: %s",
            MIN_PASSWORD, username, password,
        )
    else:
        log.info("Created admin user '%s' from ADMIN_USER / ADMIN_PASSWORD", username)


# --- very small in-memory login throttle -------------------------------------
_failures: dict[str, list[float]] = {}
MAX_FAILURES = 8
WINDOW = 600


def check_throttle(key: str):
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < WINDOW]
    _failures[key] = recent
    if len(recent) >= MAX_FAILURES:
        raise HTTPException(429, "Too many failed logins. Try again in a few minutes.")


def record_failure(key: str):
    _failures.setdefault(key, []).append(time.time())
    if len(_failures) > 5000:
        _failures.clear()
