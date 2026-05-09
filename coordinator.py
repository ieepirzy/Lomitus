#!/usr/bin/env python3
"""
coordinator.py — multi-agent file lock coordinator, v0
PreToolUse hook for Claude Code. Reads tool context from stdin (JSON),
acquires/checks file locks in SQLite, exits 0 (allow) or 1 (block).

Usage in .claude/settings.json:
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write|MultiEdit",
      "hooks": [{"type": "command", "command": "python /path/to/coordinator.py"}]
    }]
  }
}
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(".claude/coordinator.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS locks (
    file_path   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    agent_type  TEXT,
    acquired_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (file_path, agent_id)
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id    TEXT PRIMARY KEY,
    agent_type  TEXT,
    session_id  TEXT,
    registered_at TEXT DEFAULT (datetime('now'))
);
"""


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def normalize_path(raw: str) -> str:
    """Resolve to absolute path so worktree variants collapse to one canonical key."""
    return str(Path(raw).resolve())


def parse_hook_input() -> dict:
    return json.load(sys.stdin)


def extract_context(payload: dict) -> tuple[str, str, str | None, str | None]:
    """
    Returns (file_path, agent_id, agent_type, session_id).
    agent_id falls back to session_id when not in a subagent context.
    """
    tool_input = payload.get("tool_input", {})

    # Claude Code hook payload fields
    session_id = payload.get("session_id", "unknown-session")
    agent_id    = payload.get("agent_id") or session_id   # absent → main agent
    agent_type  = payload.get("agent_type")               # None for main agent

    # Extract file path depending on tool name
    tool_name = payload.get("tool_name", "")
    if tool_name == "MultiEdit":
        file_path = tool_input.get("file_path", "")
    else:
        file_path = tool_input.get("file_path", "")

    return normalize_path(file_path), agent_id, agent_type, session_id


def try_acquire(conn: sqlite3.Connection, file_path: str, agent_id: str, agent_type: str | None) -> str | None:
    """
    Attempt to insert a lock row.
    Returns None on success, or the blocking agent_id on conflict.
    """
    try:
        conn.execute(
            "INSERT INTO locks (file_path, agent_id, agent_type) VALUES (?, ?, ?)",
            (file_path, agent_id, agent_type),
        )
        conn.commit()
        return None
    except sqlite3.IntegrityError:
        # PRIMARY KEY conflict → file already locked by someone
        row = conn.execute(
            "SELECT agent_id FROM locks WHERE file_path = ? AND agent_id != ?",
            (file_path, agent_id),
        ).fetchone()
        return row[0] if row else None


def release_locks(conn: sqlite3.Connection, agent_id: str) -> None:
    """Release all locks held by this agent. Called on PostToolUse (future)."""
    conn.execute("DELETE FROM locks WHERE agent_id = ?", (agent_id,))
    conn.commit()


def block(reason: str) -> None:
    print(reason, file=sys.stderr)
    sys.exit(2)


def allow() -> None:
    # Exit 0, no output needed
    sys.exit(0)


def main() -> None:

    if "--release" in sys.argv:
        try:
            payload = parse_hook_input()
            _, agent_id, _, _ = extract_context(payload)
            conn = get_db()
            release_locks(conn, agent_id)
        except Exception:
            pass  # release failures are non-critical
        sys.exit(0)

    try:
        payload = parse_hook_input()
    except (json.JSONDecodeError, Exception) as e:
        # Block on coordinator failure, prevents silent collisions that surface in merge conflicts
        block("Coordinator error: cannot guarantee safe access.")

    file_path, agent_id, agent_type, session_id = extract_context(payload)

    if not file_path or file_path == str(Path("").resolve()):
        allow()

    try:
        conn = get_db()
    except Exception:
        # DB unavailable fail closed as above
        block("Coordinator error: cannot guarantee safe access.")

    blocker = try_acquire(conn, file_path, agent_id, agent_type)

    if blocker:
        block(
            f"File locked by agent '{blocker}'. "
            f"Wait for that agent to finish or choose a different file."
        )
    else:
        allow()


if __name__ == "__main__":
    main()