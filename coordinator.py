#!/usr/bin/env python3
"""
coordinator.py — multi-agent subgraph lock coordinator, v1.

Reads hook context from stdin (JSON). Exits 0 (allow) or 2 (block).

Flags:
  (none)     PreToolUse         — conflict check, lock acquisition, subgraph snapshot
  --release  PostToolUse        — drift check, global cache update, lock release
  --failure  PostToolUseFailure — silent lock release, no cache update

Usage in .claude/settings.json:
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write|MultiEdit",
      "hooks": [{"type": "command", "command": "python /path/to/coordinator.py"}]
    }],
    "PostToolUse": [{
      "matcher": "Edit|Write|MultiEdit",
      "hooks": [{"type": "command", "command": "python /path/to/coordinator.py --release"}]
    }],
    "PostToolUseFailure": [{
      "matcher": "Edit|Write|MultiEdit",
      "hooks": [{"type": "command", "command": "python /path/to/coordinator.py --failure"}]
    }]
  }
}
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from dep_graph import (
    compute_merkle_root,
    compute_merkle_root_from_node_ids,
    identify_target_nodes,
    index_file,
    is_fresh,
    crawl_subgraph,
    update_file,
)
from bloom import load_bloom, save_bloom

DB_PATH = Path(".claude/coordinator.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    agent_type    TEXT,
    session_id    TEXT,
    registered_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS locks (
    node_id        TEXT PRIMARY KEY,
    file_path      TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    agent_type     TEXT,
    subgraph_hash  TEXT,
    subgraph_nodes TEXT,
    acquired_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id      TEXT PRIMARY KEY,
    file_path    TEXT NOT NULL,
    name         TEXT,
    kind         TEXT NOT NULL,
    line_start   INTEGER NOT NULL,
    line_end     INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    from_file    TEXT NOT NULL,
    to_file      TEXT NOT NULL,
    PRIMARY KEY (from_file, to_file)
);

CREATE TABLE IF NOT EXISTS bloom_state (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    bitarray BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS worktrees (
    worktree_path TEXT PRIMARY KEY,
    agent_id      TEXT,
    session_id    TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_locks_file ON locks(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_file);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges(to_file);
"""


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    # v0 → v1: locks PK changed from file_path to node_id — drop and recreate.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(locks)").fetchall()}
    if cols and "node_id" not in cols:
        conn.execute("DROP TABLE IF EXISTS locks")
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_project_root(start: str) -> str:
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for ancestor in (p, *p.parents):
        if (ancestor / ".git").exists():
            return str(ancestor)
    return str(Path(__file__).parent)


def is_indexed(file_path: str, conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM nodes WHERE file_path = ? LIMIT 1", (file_path,)
    ).fetchone() is not None


def parse_hook_input() -> dict:
    return json.load(sys.stdin)


def extract_context(payload: dict) -> tuple[str, str, str | None, str | None, str, dict]:
    """Returns (file_path, agent_id, agent_type, session_id, tool_name, tool_input)."""
    tool_input = payload.get("tool_input", {})
    session_id = payload.get("session_id", "unknown-session")
    agent_id   = payload.get("agent_id") or session_id
    agent_type = payload.get("agent_type")
    tool_name  = payload.get("tool_name", "")
    file_path  = str(Path(tool_input.get("file_path", "")).resolve())
    return file_path, agent_id, agent_type, session_id, tool_name, tool_input


def block(reason: str) -> None:
    print(reason, file=sys.stderr)
    sys.exit(2)


def allow() -> None:
    sys.exit(0)


def _in_clause(items: list | set) -> tuple[str, list]:
    """Return (placeholders, values) for a SQL IN clause."""
    lst = list(items)
    return ",".join("?" * len(lst)), lst


def _file_sentinel(file_path: str) -> str:
    """
    Synthetic node_id used as a file-level lock when Write targets a file
    with no existing structural nodes (empty or brand-new file).
    Not stored in the nodes table — only in locks.
    """
    return f"{file_path}::__file__"


def _is_sentinel(node_id: str) -> bool:
    return node_id.endswith("::__file__")


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------

def handle_pretool(
    file_path: str,
    agent_id: str,
    agent_type: str | None,
    tool_name: str,
    tool_input: dict,
    conn: sqlite3.Connection,
) -> None:
    project_root = find_project_root(file_path)

    # 1. Cold cache: ensure target is indexed before any node lookups.
    if not is_indexed(file_path, conn):
        index_file(file_path, project_root, conn)

    # 2. Identify which nodes this edit targets; filter to structural only.
    target_node_ids = identify_target_nodes(file_path, tool_name, tool_input, conn)
    if target_node_ids:
        ph, vals = _in_clause(target_node_ids)
        structural_targets = [
            r[0] for r in conn.execute(
                f"SELECT node_id FROM nodes WHERE node_id IN ({ph}) AND kind = 'structural'",
                vals,
            ).fetchall()
        ]
    else:
        structural_targets = []

    # Write replaces the entire file: always take a file-level sentinel lock.
    # This covers empty files and brand-new files where no structural nodes exist yet,
    # ensuring the first writer wins and subsequent agents see the post-write state.
    if tool_name == "Write" and not structural_targets:
        structural_targets = [_file_sentinel(file_path)]

    # Edit/MultiEdit outside every named node (whitespace, comment, between functions):
    # allow without lock — unless another agent holds a structural lock on this file.
    # A benign edit (e.g. removing an import) can invalidate a concurrent structural edit
    # being reasoned about by the lock holder.
    if not structural_targets:
        other_lock = conn.execute(
            "SELECT agent_id FROM locks WHERE file_path = ? AND agent_id != ? LIMIT 1",
            (file_path, agent_id),
        ).fetchone()
        if other_lock:
            block(
                f"Benign edit on '{Path(file_path).name}' blocked — "
                f"'{other_lock[0]}' holds a structural lock on this file. Replan."
            )
        allow()

    # 3. Crawl 1 edge forward (enforcement depth) and cold-cache any unseen deps.
    dep_files = crawl_subgraph(file_path, depth=1, conn=conn, direction="forward") - {file_path}
    for dep in dep_files:
        if not is_indexed(dep, conn):
            index_file(dep, project_root, conn)

    # 4. Conflict check — bloom pre-filter then SQLite confirm.
    #    Collect all conflicts before blocking so the agent gets the full picture.
    bloom = load_bloom(conn)

    def check_locked_by_other(node_id: str) -> str | None:
        if bloom.might_contain(node_id):
            row = conn.execute(
                "SELECT agent_id FROM locks WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row and row[0] != agent_id:
                return row[0]
        return None

    # Check target nodes.
    target_conflicts = [(nid, check_locked_by_other(nid)) for nid in structural_targets]
    target_conflicts = [(nid, b) for nid, b in target_conflicts if b]

    # Check structural nodes in direct dependencies.
    dep_conflicts: list[tuple[str, str]] = []
    if dep_files:
        ph, vals = _in_clause(dep_files)
        dep_structural = conn.execute(
            f"SELECT node_id FROM nodes WHERE file_path IN ({ph}) AND kind = 'structural'",
            vals,
        ).fetchall()
        for (dep_nid,) in dep_structural:
            blocker = check_locked_by_other(dep_nid)
            if blocker:
                dep_conflicts.append((dep_nid, blocker))

    all_conflicts = target_conflicts + dep_conflicts
    if all_conflicts:
        details = "; ".join(f"'{nid}' held by '{b}'" for nid, b in all_conflicts)
        block(f"Lock conflict — {details}. Replan.")

    # 5. Compute subgraph snapshot for drift detection at PostToolUse.
    all_files = {file_path} | dep_files
    ph, vals = _in_clause(all_files)
    subgraph_node_ids = [
        r[0] for r in conn.execute(
            f"SELECT node_id FROM nodes WHERE file_path IN ({ph})", vals
        ).fetchall()
    ]
    subgraph_hash = compute_merkle_root(all_files, conn)

    # 6. Acquire locks — single transaction, all-or-nothing.
    with conn:
        for nid in structural_targets:
            try:
                conn.execute(
                    "INSERT INTO locks "
                    "(node_id, file_path, agent_id, agent_type, subgraph_hash, subgraph_nodes) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (nid, file_path, agent_id, agent_type,
                     subgraph_hash, json.dumps(subgraph_node_ids)),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT agent_id FROM locks WHERE node_id = ?", (nid,)
                ).fetchone()
                if row and row[0] != agent_id:
                    # Bloom false negative — another agent acquired between check and insert.
                    block(f"Node '{nid}' locked by '{row[0]}' (race). Replan.")
                # Same agent re-entering: refresh the snapshot.
                conn.execute(
                    "UPDATE locks SET subgraph_hash = ?, subgraph_nodes = ? WHERE node_id = ?",
                    (subgraph_hash, json.dumps(subgraph_node_ids), nid),
                )

    # 7. Update bloom after successful lock acquisition.
    for nid in structural_targets:
        bloom.add(nid)
    save_bloom(conn, bloom)

    allow()


# ---------------------------------------------------------------------------
# PostToolUse
# ---------------------------------------------------------------------------

def handle_release(
    file_path: str,
    agent_id: str,
    conn: sqlite3.Connection,
) -> None:
    rows = conn.execute(
        "SELECT node_id, subgraph_hash, subgraph_nodes FROM locks "
        "WHERE file_path = ? AND agent_id = ?",
        (file_path, agent_id),
    ).fetchall()

    if not rows:
        sys.exit(0)  # idempotent

    project_root = find_project_root(file_path)
    bloom = load_bloom(conn)

    for row in rows:
        node_id, stored_hash, subgraph_nodes_json = row

        # Sentinel locks (Write on empty file) carry no subgraph snapshot — no drift
        # check possible, and none needed: the file was empty, nothing could have drifted.
        if _is_sentinel(node_id):
            continue

        stored_nodes = json.loads(subgraph_nodes_json) if subgraph_nodes_json else []
        recomputed   = compute_merkle_root_from_node_ids(stored_nodes, conn)

        if recomputed != stored_hash:
            try:
                raise NotImplementedError("rollback not implemented — v2")
            except NotImplementedError as e:
                print(f"Coordinator [v2-stub]: {e}", file=sys.stderr)
            print(
                f"Subgraph drifted for '{node_id}': a dependency was mutated during the edit "
                "window. Edit may be built on stale state. Replan against current dependencies.",
                file=sys.stderr,
            )
            conn.execute(
                "DELETE FROM locks WHERE file_path = ? AND agent_id = ?",
                (file_path, agent_id),
            )
            conn.commit()
            for r in rows:
                bloom.remove(r[0])
            save_bloom(conn, bloom)
            sys.exit(2)

    # Subgraph is clean — update global cache first, then release lock.
    # update_file also handles the new-nodes case: if Write added functions to a previously
    # empty file, they are indexed here and visible to subsequent agents.
    update_file(file_path, project_root, conn)

    conn.execute(
        "DELETE FROM locks WHERE file_path = ? AND agent_id = ?",
        (file_path, agent_id),
    )
    conn.commit()
    for row in rows:
        bloom.remove(row[0])
    save_bloom(conn, bloom)

    sys.exit(0)


# ---------------------------------------------------------------------------
# PostToolUseFailure
# ---------------------------------------------------------------------------

def handle_failure(file_path: str, agent_id: str, conn: sqlite3.Connection) -> None:
    """Edit failed — release lock without updating cache. Previous hashes remain valid."""
    locked_nodes = conn.execute(
        "SELECT node_id FROM locks WHERE file_path = ? AND agent_id = ?",
        (file_path, agent_id),
    ).fetchall()

    conn.execute(
        "DELETE FROM locks WHERE file_path = ? AND agent_id = ?",
        (file_path, agent_id),
    )
    conn.commit()

    bloom = load_bloom(conn)
    for (node_id,) in locked_nodes:
        bloom.remove(node_id)
    save_bloom(conn, bloom)

    sys.exit(0)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _release_agent_locks(agent_id: str, conn: sqlite3.Connection) -> None:
    """Release all locks held by agent_id without updating cache (no edit confirmation)."""
    locked = conn.execute(
        "SELECT node_id FROM locks WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    conn.execute("DELETE FROM locks WHERE agent_id = ?", (agent_id,))
    conn.commit()
    if locked:
        bloom = load_bloom(conn)
        for (nid,) in locked:
            bloom.remove(nid)
        save_bloom(conn, bloom)


def _warm_from_file(file_path: str, depth: int, project_root: str, conn: sqlite3.Connection) -> None:
    """Re-index file_path if stale, then warm depth edges forward.
    Skips re-indexing when hashes already match — guards against double-indexing
    when PostToolUse runs before a FileChanged event fires for the same edit."""
    if not Path(file_path).is_file():
        return
    if not is_fresh(file_path, conn):
        update_file(file_path, project_root, conn)
    dep_files = crawl_subgraph(file_path, depth=depth, conn=conn, direction="forward")
    for f in dep_files - {file_path}:
        if not is_indexed(f, conn) and Path(f).is_file():
            try:
                index_file(f, project_root, conn)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Passive hook handlers
# ---------------------------------------------------------------------------

def handle_session_start(_payload: dict, _conn: sqlite3.Connection) -> None:
    # DB is already initialized by get_db().
    sys.exit(0)


def handle_session_end(session_id: str, conn: sqlite3.Connection) -> None:
    locked = conn.execute("SELECT node_id FROM locks").fetchall()
    conn.execute("DELETE FROM locks")
    conn.execute("DELETE FROM agents WHERE session_id = ?", (session_id,))
    conn.commit()
    if locked:
        bloom = load_bloom(conn)
        for (nid,) in locked:
            bloom.remove(nid)
        save_bloom(conn, bloom)
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    sys.exit(0)


def handle_subagent_start(payload: dict, conn: sqlite3.Connection) -> None:
    agent_id      = payload.get("agent_id") or payload.get("session_id", "unknown")
    agent_type    = payload.get("agent_type")
    session_id    = payload.get("session_id", "")
    worktree_path = payload.get("worktree_path", "")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO agents (agent_id, agent_type, session_id) VALUES (?, ?, ?)",
            (agent_id, agent_type, session_id),
        )
        if worktree_path:
            conn.execute(
                "INSERT OR REPLACE INTO worktrees (worktree_path, agent_id, session_id) VALUES (?, ?, ?)",
                (worktree_path, agent_id, session_id),
            )
    sys.exit(0)


def handle_subagent_stop(payload: dict, conn: sqlite3.Connection) -> None:
    agent_id = payload.get("agent_id") or payload.get("session_id", "unknown")
    _release_agent_locks(agent_id, conn)
    sys.exit(0)


def handle_file_changed(payload: dict, conn: sqlite3.Connection) -> None:
    file_path = str(Path(payload.get("file_path", "")).resolve())
    if not file_path or file_path == str(Path("").resolve()):
        sys.exit(0)
    project_root = find_project_root(file_path)
    _warm_from_file(file_path, depth=2, project_root=project_root, conn=conn)
    sys.exit(0)


def handle_cwd_changed(payload: dict, conn: sqlite3.Connection) -> None:
    cwd = payload.get("cwd", "")
    if not cwd:
        sys.exit(0)
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        sys.exit(0)
    project_root = find_project_root(str(cwd_path))
    for py_file in cwd_path.glob("*.py"):
        try:
            _warm_from_file(str(py_file.resolve()), depth=2, project_root=project_root, conn=conn)
        except Exception:
            continue
    sys.exit(0)


def handle_worktree_create(payload: dict, conn: sqlite3.Connection) -> None:
    worktree_path = payload.get("worktree_path", "")
    if not worktree_path:
        sys.exit(0)
    agent_id   = payload.get("agent_id") or payload.get("session_id", "unknown")
    session_id = payload.get("session_id", "")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO worktrees (worktree_path, agent_id, session_id) VALUES (?, ?, ?)",
            (worktree_path, agent_id, session_id),
        )
    sys.exit(0)


def handle_worktree_remove(payload: dict, conn: sqlite3.Connection) -> None:
    worktree_path = payload.get("worktree_path", "")
    if not worktree_path:
        sys.exit(0)
    rows = conn.execute(
        "SELECT agent_id FROM worktrees WHERE worktree_path = ?", (worktree_path,)
    ).fetchall()
    for (agent_id,) in rows:
        _release_agent_locks(agent_id, conn)
    with conn:
        conn.execute("DELETE FROM worktrees WHERE worktree_path = ?", (worktree_path,))
    sys.exit(0)


def handle_post_tool_batch(_conn: sqlite3.Connection) -> None:
    # v1 no-op: batch-level validation is a v2 concern.
    sys.exit(0)


def handle_pre_compact(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = set(sys.argv[1:])

    if   "--session-start"   in args: mode = "session_start"
    elif "--session-end"     in args: mode = "session_end"
    elif "--subagent-start"  in args: mode = "subagent_start"
    elif "--subagent-stop"   in args: mode = "subagent_stop"
    elif "--file-changed"    in args: mode = "file_changed"
    elif "--cwd-changed"     in args: mode = "cwd_changed"
    elif "--worktree-create" in args: mode = "worktree_create"
    elif "--worktree-remove" in args: mode = "worktree_remove"
    elif "--post-tool-batch" in args: mode = "post_tool_batch"
    elif "--pre-compact"     in args: mode = "pre_compact"
    elif "--release"         in args: mode = "release"
    elif "--failure"         in args: mode = "failure"
    else:                             mode = "pretool"

    try:
        payload = parse_hook_input()
    except Exception:
        if mode == "pretool":
            block("Coordinator error: cannot parse hook payload.")
        sys.exit(0)

    try:
        conn = get_db()
    except Exception:
        if mode == "pretool":
            block("Coordinator error: DB unavailable.")
        sys.exit(0)

    if mode == "pretool":
        file_path, agent_id, agent_type, session_id, tool_name, tool_input = extract_context(payload)
        empty_path = str(Path("").resolve())
        if not file_path or file_path == empty_path:
            sys.exit(0)
        handle_pretool(file_path, agent_id, agent_type, tool_name, tool_input, conn)
    elif mode == "release":
        file_path, agent_id, *_ = extract_context(payload)
        handle_release(file_path, agent_id, conn)
    elif mode == "failure":
        file_path, agent_id, *_ = extract_context(payload)
        handle_failure(file_path, agent_id, conn)
    elif mode == "session_start":
        handle_session_start(payload, conn)
    elif mode == "session_end":
        handle_session_end(payload.get("session_id", ""), conn)
    elif mode == "subagent_start":
        handle_subagent_start(payload, conn)
    elif mode == "subagent_stop":
        handle_subagent_stop(payload, conn)
    elif mode == "file_changed":
        handle_file_changed(payload, conn)
    elif mode == "cwd_changed":
        handle_cwd_changed(payload, conn)
    elif mode == "worktree_create":
        handle_worktree_create(payload, conn)
    elif mode == "worktree_remove":
        handle_worktree_remove(payload, conn)
    elif mode == "post_tool_batch":
        handle_post_tool_batch(conn)
    elif mode == "pre_compact":
        handle_pre_compact(conn)


if __name__ == "__main__":
    main()
