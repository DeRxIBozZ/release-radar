import os
import sqlite3
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "release-radar.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    prefs TEXT NOT NULL DEFAULT '{}',
    filters TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS titles (
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    overview TEXT,
    poster_path TEXT,
    genres TEXT NOT NULL DEFAULT '[]',
    popularity REAL NOT NULL DEFAULT 0,
    vote_average REAL NOT NULL DEFAULT 0,
    vote_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (media_type, tmdb_id)
);
CREATE TABLE IF NOT EXISTS releases (
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    region TEXT NOT NULL,
    source TEXT NOT NULL,          -- 'theatrical' | 'digital' | 'p:<provider id>'
    date TEXT NOT NULL,            -- YYYY-MM-DD
    season INTEGER NOT NULL DEFAULT 0,
    ep_from INTEGER NOT NULL DEFAULT 0,
    ep_to INTEGER NOT NULL DEFAULT 0,
    ep_total INTEGER NOT NULL DEFAULT 0,
    is_premiere INTEGER NOT NULL DEFAULT 0,
    is_rerelease INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (media_type, tmdb_id, region, source, date, season, ep_from)
);
CREATE INDEX IF NOT EXISTS releases_date ON releases(date);
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    logo_path TEXT,
    regions TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 999
);
CREATE TABLE IF NOT EXISTS genres (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def tx():
    """Open a connection, commit on success, roll back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    os.makedirs(DATA_DIR, exist_ok=True)
    with tx() as conn:
        conn.executescript(SCHEMA)
        # Migrations for databases created by older versions
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(titles)")}
        if "vote_count" not in cols:
            conn.execute("ALTER TABLE titles ADD COLUMN vote_count INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(releases)")}
        if "is_rerelease" not in cols:
            conn.execute("ALTER TABLE releases ADD COLUMN is_rerelease INTEGER NOT NULL DEFAULT 0")
        if "note" not in cols:
            conn.execute("ALTER TABLE releases ADD COLUMN note TEXT NOT NULL DEFAULT ''")


def get_meta(key, default=None):
    with tx() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key, value):
    with tx() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
