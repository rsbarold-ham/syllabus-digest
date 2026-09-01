import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "app.db"

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
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    due_date TEXT NOT NULL,
    title TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assignments_user ON assignments(user_id);
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
]


def init_db():
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
