#!/usr/bin/env python3
"""
watchdog.py — TTL enforcement for stale coordinator locks.

Polls the locks table for expired rows (expires_at < now), reverts each
affected file to its pre-lock snapshot, releases the lock, and updates the
bloom filter. Marks any in-progress cascades belonging to expired agents
as reverted.

Run alongside the coordinator for the duration of a session:
    python watchdog.py [--poll-interval N]   # default: 10 seconds
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

POLL_DEFAULT = 10  # seconds


def _find_db_path() -> Path:
    """Locate the coordinator DB in the main repo root, same logic as coordinator.get_db()."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, cwd=str(Path.cwd()),
        )
        if result.returncode == 0:
            common = Path(result.stdout.strip())
            if not common.is_absolute():
                common = (Path.cwd() / common).resolve()
            return common.parent / ".claude" / "coordinator.db"
    except Exception:
        pass
    return Path(".claude/coordinator.db")


def _revert_file(file_path: str, source_content: str) -> None:
    Path(file_path).write_text(source_content, encoding="utf-8")


def _sweep(conn: sqlite3.Connection) -> None:
    # Only expire locks with an explicit TTL; cascade locks (expires_at IS NULL) are exempt.
    expired = conn.execute(
        "SELECT node_id, file_path, agent_id FROM locks "
        "WHERE expires_at IS NOT NULL AND expires_at < datetime('now')"
    ).fetchall()
    if not expired:
        return

    agent_ids = {agent_id for _, _, agent_id in expired}

    # Revert files from snapshots before releasing locks.
    reverted: set[str] = set()
    for node_id, file_path, _ in expired:
        if file_path in reverted:
            continue
        snap = conn.execute(
            "SELECT source_content FROM snapshots WHERE node_id = ?", (node_id,)
        ).fetchone()
        if snap and snap[0]:
            try:
                _revert_file(file_path, snap[0])
                reverted.add(file_path)
                print(
                    f"watchdog: reverted '{file_path}' from snapshot (TTL expired, node '{node_id}')",
                    file=sys.stderr,
                )
            except OSError as e:
                print(f"watchdog: revert failed for '{file_path}': {e}", file=sys.stderr)

    # Release expired locks and their queue entries.
    with conn:
        conn.execute(
            "DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at < datetime('now')"
        )
        for agent_id in agent_ids:
            conn.execute("DELETE FROM lock_queue WHERE agent_id = ?", (agent_id,))

    # Mark in-progress cascades for crashed agents as reverted.
    with conn:
        for agent_id in agent_ids:
            conn.execute(
                "UPDATE cascades SET status = 'reverted' "
                "WHERE agent_id = ? AND status = 'in_progress'",
                (agent_id,),
            )


def main() -> None:
    interval = POLL_DEFAULT
    if "--poll-interval" in sys.argv:
        idx = sys.argv.index("--poll-interval")
        try:
            interval = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            pass

    db_path = _find_db_path()
    print(f"watchdog: polling '{db_path}' every {interval}s", file=sys.stderr)

    while True:
        try:
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                conn.execute("PRAGMA journal_mode=WAL")
                _sweep(conn)
                conn.close()
        except Exception as e:
            print(f"watchdog: sweep error: {e}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    main()
