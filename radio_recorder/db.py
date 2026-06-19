from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from flask import current_app


SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL UNIQUE,
    stream_url TEXT NOT NULL,
    mastodon_url TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE RESTRICT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
    artwork_path TEXT,
    frequency TEXT NOT NULL CHECK(frequency IN ('daily', 'weekly')),
    start_time TEXT NOT NULL,
    weekday INTEGER CHECK(weekday BETWEEN 0 AND 6),
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    mp3_path TEXT,
    playlist_path TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(show_id, scheduled_at)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db(app) -> None:
    with app.app_context(), closing(connect()) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('final_dir', ?)",
            (str(app.config["FINAL_DIR"]),),
        )
        db.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def query(sql: str, params: tuple = (), one: bool = False):
    with closing(connect()) as db:
        rows = db.execute(sql, params).fetchall()
        return (rows[0] if rows else None) if one else rows


def execute(sql: str, params: tuple = ()) -> int:
    with closing(connect()) as db:
        cursor = db.execute(sql, params)
        db.commit()
        return cursor.lastrowid

