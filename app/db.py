import os
import sqlite3
from pathlib import Path

# Defaults to sitting next to the app's own code, which is fine for local
# dev but sits on the *ephemeral* part of the filesystem on most hosts --
# it gets wiped on every redeploy/restart. Set DB_PATH to a file on a
# persistent disk (e.g. Render's paid persistent-disk mount) to survive
# those. See README for the exact steps.
DB_PATH = Path(os.environ.get("DB_PATH") or (Path(__file__).resolve().parent.parent / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    digest_hour INTEGER NOT NULL DEFAULT 6,
    digest_enabled INTEGER NOT NULL DEFAULT 1,
    smtp_email TEXT,
    smtp_app_password_encrypted TEXT,
    magic_token TEXT UNIQUE,
    reset_token TEXT,
    reset_token_expires TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    due_date TEXT NOT NULL,
    title TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    urgent INTEGER NOT NULL DEFAULT 0,
    urgent_marked_at TEXT,
    urgent_reminder_sent INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_assignments_user ON assignments(user_id);

CREATE TABLE IF NOT EXISTS course_colors (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    color_hex TEXT NOT NULL,
    PRIMARY KEY (user_id, tag)
);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added to `users` after the table may already exist in a deployed
# database. `CREATE TABLE IF NOT EXISTS` above only helps on a fresh
# database -- an already-existing `users` table needs an explicit ALTER TABLE
# for each new column, or every query touching it fails with "no such column".
MIGRATIONS = [
    ("users", "magic_token", "ALTER TABLE users ADD COLUMN magic_token TEXT"),
    ("assignments", "completed", "ALTER TABLE assignments ADD COLUMN completed INTEGER NOT NULL DEFAULT 0"),
    ("assignments", "urgent", "ALTER TABLE assignments ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0"),
    ("assignments", "urgent_marked_at", "ALTER TABLE assignments ADD COLUMN urgent_marked_at TEXT"),
    ("assignments", "urgent_reminder_sent", "ALTER TABLE assignments ADD COLUMN urgent_reminder_sent INTEGER NOT NULL DEFAULT 0"),
    ("users", "reset_token", "ALTER TABLE users ADD COLUMN reset_token TEXT"),
    ("users", "reset_token_expires", "ALTER TABLE users ADD COLUMN reset_token_expires TEXT"),
]


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()

        for table, column, alter_sql in MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(alter_sql)
        conn.commit()
    finally:
        conn.close()
