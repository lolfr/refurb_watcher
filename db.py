"""refurb_watcher — database layer.

Two interchangeable backends, selected by `[database] backend` in config.toml:

  - "sqlite"  (default) : zero-setup, single file, schema auto-created on first
                          connect. Nothing to install (Python stdlib).
  - "mariadb" (optional): for an existing MariaDB/MySQL. Requires
                          `pip install pymysql` and applying schema.sql once.

All queries are written with `?` placeholders and translated to the backend's
placeholder at execution time, so the rest of the codebase is backend-agnostic.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger(__name__)

# Python 3.12 removed the default sqlite3 datetime adapters/converters, so we
# register explicit ones (store as ISO text, read back as datetime objects —
# matching what the MariaDB DictCursor returns, so callers don't special-case).
sqlite3.register_adapter(dt.datetime, lambda d: d.isoformat(sep=" "))
sqlite3.register_converter("timestamp", lambda b: dt.datetime.fromisoformat(b.decode()))

# Auto-created for the SQLite backend (idempotent). The MariaDB equivalent lives
# in schema.sql (applied manually, since it may target a shared database).
_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS refurb_snapshot (
    part_number      TEXT      PRIMARY KEY,
    category         TEXT      NOT NULL,
    title            TEXT      NOT NULL,
    price_cents      INTEGER   NOT NULL,
    list_price_cents INTEGER,
    savings_cents    INTEGER,
    currency         TEXT      NOT NULL DEFAULT 'EUR',
    url              TEXT      NOT NULL,
    ram_gb           INTEGER,
    storage_gb       INTEGER,
    chip             TEXT,
    cpu_cores        INTEGER,
    gpu_cores        INTEGER,
    raw_specs        TEXT,
    first_seen       timestamp NOT NULL,
    last_seen        timestamp NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_category ON refurb_snapshot(category);
CREATE INDEX IF NOT EXISTS idx_snapshot_last_seen ON refurb_snapshot(last_seen);

CREATE TABLE IF NOT EXISTS refurb_events (
    id          INTEGER   PRIMARY KEY AUTOINCREMENT,
    part_number TEXT      NOT NULL,
    event_type  TEXT      NOT NULL,   -- appeared | disappeared | price_changed
    seen_at     timestamp NOT NULL,
    price_cents INTEGER,
    title       TEXT,
    notified    INTEGER   NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_part ON refurb_events(part_number);
CREATE INDEX IF NOT EXISTS idx_events_seen ON refurb_events(seen_at);

CREATE TABLE IF NOT EXISTS refurb_polls (
    id             INTEGER   PRIMARY KEY AUTOINCREMENT,
    polled_at      timestamp NOT NULL,
    url            TEXT      NOT NULL,
    http_status    INTEGER,
    products_found INTEGER,
    error          TEXT,
    duration_ms    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_polls_polled ON refurb_polls(polled_at);
"""

# Columns written on every upsert (order matters: matches _UPSERT values).
_SNAPSHOT_COLS = [
    "part_number", "category", "title", "price_cents", "list_price_cents",
    "savings_cents", "currency", "url", "ram_gb", "storage_gb", "chip",
    "cpu_cores", "gpu_cores", "raw_specs", "first_seen", "last_seen",
]
# Columns refreshed when the product already exists (first_seen is preserved).
_SNAPSHOT_UPDATE_COLS = [
    "title", "price_cents", "list_price_cents", "savings_cents", "url",
    "ram_gb", "storage_gb", "chip", "cpu_cores", "gpu_cores", "raw_specs",
    "last_seen",
]


class DB:
    """Thin connection wrapper that hides the backend's placeholder style and
    always returns rows as plain (mutable) dicts."""

    def __init__(self, conn, placeholder: str, is_sqlite: bool):
        self._conn = conn
        self._ph = placeholder
        self.is_sqlite = is_sqlite

    def execute(self, sql: str, params=()):
        cur = self._conn.cursor()
        cur.execute(sql.replace("?", self._ph), params)
        return cur

    def query(self, sql: str, params=()) -> list[dict]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


@contextmanager
def connect(cfg: dict) -> Iterator[DB]:
    """Open a connection (auto-commit on success, rollback on exception)."""
    backend = (cfg.get("backend") or "sqlite").lower()
    if backend == "mariadb":
        import pymysql
        import pymysql.cursors

        raw = pymysql.connect(
            host=cfg["host"],
            port=int(cfg.get("port", 3306)),
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
        db = DB(raw, "%s", is_sqlite=False)
    elif backend == "sqlite":
        raw = sqlite3.connect(
            cfg.get("path", "refurb.db"),
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        raw.row_factory = sqlite3.Row
        raw.executescript(_SCHEMA_SQLITE)  # idempotent: creates tables on first run
        db = DB(raw, "?", is_sqlite=True)
    else:
        raise ValueError(f"unknown database backend: {backend!r} (use 'sqlite' or 'mariadb')")

    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------- Snapshot (current products) ----------

def current_snapshot(db: DB) -> dict[str, dict]:
    """Current product state, indexed by part_number."""
    return {row["part_number"]: row for row in db.query("SELECT * FROM refurb_snapshot")}


def upsert_product(db: DB, product: dict, now: dt.datetime, is_new: bool = False) -> None:
    """Insert a new product or refresh an existing one (first_seen preserved)."""
    values = [product.get(c) for c in _SNAPSHOT_COLS[:-2]] + [now, now]
    placeholders = ", ".join(["?"] * len(_SNAPSHOT_COLS))
    if db.is_sqlite:
        set_clause = ", ".join(f"{c}=excluded.{c}" for c in _SNAPSHOT_UPDATE_COLS)
        conflict = f"ON CONFLICT(part_number) DO UPDATE SET {set_clause}"
    else:
        set_clause = ", ".join(f"{c}=VALUES({c})" for c in _SNAPSHOT_UPDATE_COLS)
        conflict = f"ON DUPLICATE KEY UPDATE {set_clause}"
    sql = (
        f"INSERT INTO refurb_snapshot ({', '.join(_SNAPSHOT_COLS)}) "
        f"VALUES ({placeholders}) {conflict}"
    )
    db.execute(sql, values)


def delete_disappeared(db: DB, current_part_numbers: set[str]) -> list[dict]:
    """Remove products no longer listed; return the rows that disappeared."""
    if current_part_numbers:
        placeholders = ", ".join(["?"] * len(current_part_numbers))
        disappeared = db.query(
            f"SELECT * FROM refurb_snapshot WHERE part_number NOT IN ({placeholders})",
            tuple(current_part_numbers),
        )
    else:
        # empty snapshot received: treat everything as gone
        disappeared = db.query("SELECT * FROM refurb_snapshot")
    for row in disappeared:
        db.execute("DELETE FROM refurb_snapshot WHERE part_number = ?", (row["part_number"],))
    return disappeared


# ---------- Event log + poll health ----------

def log_event(db: DB, part_number: str, event_type: str, seen_at: dt.datetime,
              price_cents: int | None, title: str | None, notified: bool) -> None:
    db.execute(
        "INSERT INTO refurb_events (part_number, event_type, seen_at, price_cents, title, notified) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (part_number, event_type, seen_at, price_cents, title, int(notified)),
    )


def log_poll(db: DB, polled_at: dt.datetime, url: str, http_status: int | None,
             products_found: int | None, error: str | None, duration_ms: int | None) -> None:
    db.execute(
        "INSERT INTO refurb_polls (polled_at, url, http_status, products_found, error, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (polled_at, url, http_status, products_found, error, duration_ms),
    )


# ---------- Read helpers (used by the dashboard) ----------

def fetch_current(db: DB) -> list[dict]:
    return db.query("SELECT * FROM refurb_snapshot ORDER BY category, price_cents")


def fetch_recent_events(db: DB, limit: int = 100) -> list[dict]:
    return db.query("SELECT * FROM refurb_events ORDER BY seen_at DESC LIMIT ?", (limit,))


def fetch_health(db: DB) -> dict:
    recent = db.query("SELECT * FROM refurb_polls ORDER BY polled_at DESC LIMIT 20")
    last = db.query(
        "SELECT MAX(polled_at) AS last_ok FROM refurb_polls "
        "WHERE http_status = 200 AND error IS NULL"
    )
    last_ok = last[0]["last_ok"] if last else None
    # SQLite skips the type converter on aggregate/computed columns, so MAX()
    # comes back as text — coerce it back to a datetime for the caller.
    if isinstance(last_ok, str):
        last_ok = dt.datetime.fromisoformat(last_ok)
    return {"recent": recent, "last_ok": last_ok}
