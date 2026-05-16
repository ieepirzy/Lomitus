import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "app.db"

CREATE_USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL UNIQUE,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    avatar_url  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.execute(CREATE_USERS_DDL)
        conn.commit()
        # Run ALTER TABLE migration if avatar_url column is absent
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "avatar_url" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")
            conn.commit()
    finally:
        conn.close()
