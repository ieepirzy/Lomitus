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
import sys
import time
from pathlib import Path

DB_PATH      = Path(".claude/coordinator.db")
POLL_DEFAULT = 10  # seconds


def _revert_file(file_path: str, source_content: str) -> None:
    Path(file_path).write_text(source_content, encoding="utf-8")


def _sweep(conn: sqlite3.Connection) -> None:
    expired = conn.execute(
        "SELECT node_id, file_path, agent_id FROM locks WHERE expires_at < datetime('now')"
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

    # Load bloom, remove expired node_ids, save.
    try:
        from bloom import load_bloom, save_bloom
        bloom = load_bloom(conn)
        for node_id, _, _ in expired:
            bloom.remove(node_id)
        save_bloom(conn, bloom)
    except Exception as e:
        print(f"watchdog: bloom update failed: {e}", file=sys.stderr)

    # Release expired locks and their queue entries.
    with conn:
        conn.execute("DELETE FROM locks WHERE expires_at < datetime('now')")
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

    print(f"watchdog: polling '{DB_PATH}' every {interval}s", file=sys.stderr)

    while True:
        try:
            if DB_PATH.exists():
                conn = sqlite3.connect(str(DB_PATH))
                conn.execute("PRAGMA journal_mode=WAL")
                _sweep(conn)
                conn.close()
        except Exception as e:
            print(f"watchdog: sweep error: {e}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    main()
