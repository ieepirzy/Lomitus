#!/usr/bin/env python3
"""
coordinator.py — multi-agent subgraph lock coordinator, v1.

Reads hook context from stdin (JSON). Exits 0 (allow) or 2 (block).

Flags:
  (none)            PreToolUse         — conflict check, lock acquisition, subgraph snapshot
  --release         PostToolUse        — drift check, global cache update, lock release
  --failure         PostToolUseFailure — silent lock release, no cache update
  --crawl-project   manual / session-start — recursively index all .py files in project root

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
import uuid as _uuid
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
from contract import capture_node_snapshot, is_superset, take_snapshot

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

CREATE TABLE IF NOT EXISTS snapshots (
    node_id        TEXT PRIMARY KEY,
    file_path      TEXT NOT NULL,
    source_content TEXT NOT NULL,
    io_snapshot    TEXT,
    io_args        TEXT,
    snapshotted_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cascades (
    cascade_id      TEXT PRIMARY KEY,
    root_node       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    cascade_nodes   TEXT NOT NULL,
    completed_nodes TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'in_progress',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lock_queue (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id   TEXT NOT NULL,
    agent_id  TEXT NOT NULL,
    priority  INTEGER NOT NULL DEFAULT 0,
    queued_at TEXT DEFAULT (datetime('now')),
    UNIQUE(node_id, agent_id)
);

CREATE TABLE IF NOT EXISTS agent_traversal_nodes (
    agent_id   TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    visited_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, node_id)
);

CREATE TABLE IF NOT EXISTS agent_traversal_edges (
    agent_id     TEXT NOT NULL,
    from_file    TEXT NOT NULL,
    to_file      TEXT NOT NULL,
    traversed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, from_file, to_file)
);

CREATE INDEX IF NOT EXISTS idx_locks_file      ON locks(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_file      ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_edges_from      ON edges(from_file);
CREATE INDEX IF NOT EXISTS idx_edges_to        ON edges(to_file);
CREATE INDEX IF NOT EXISTS idx_trav_nodes_agent ON agent_traversal_nodes(agent_id);
CREATE INDEX IF NOT EXISTS idx_trav_edges_agent ON agent_traversal_edges(agent_id);
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
    # v1 → v2: add expires_at to locks.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(locks)").fetchall()}
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE locks ADD COLUMN expires_at TEXT")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_python(file_path: str) -> bool:
    return Path(file_path).suffix == ".py"


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


# TTL heuristic: base + 2 s per line of the target function.
_BASE_TTL     = 30
_PER_LINE_TTL = 2

# Agent priority: higher value = served first from the lock queue.
_PRIORITY_MAP: dict[str | None, int] = {
    "orchestrator": 100,
    "senior": 80,
    "lead": 80,
}
_DEFAULT_PRIORITY = 0


# ---------------------------------------------------------------------------
# v2 helpers
# ---------------------------------------------------------------------------

def _compute_ttl(node_id: str, conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT line_start, line_end FROM nodes WHERE node_id = ?", (node_id,)
    ).fetchone()
    if not row:
        return _BASE_TTL
    return _BASE_TTL + max(0, row[1] - row[0]) * _PER_LINE_TTL


def _agent_priority(agent_type: str | None) -> int:
    return _PRIORITY_MAP.get(agent_type, _DEFAULT_PRIORITY)


def _enqueue_agent(node_id: str, agent_id: str, agent_type: str | None, conn: sqlite3.Connection) -> None:
    priority = _agent_priority(agent_type)
    conn.execute(
        "INSERT OR IGNORE INTO lock_queue (node_id, agent_id, priority) VALUES (?, ?, ?)",
        (node_id, agent_id, priority),
    )
    conn.commit()


def _queue_position(node_id: str, agent_id: str, conn: sqlite3.Connection) -> int:
    """Return 0-based position of agent_id in the queue for node_id (0 = front)."""
    rows = conn.execute(
        "SELECT agent_id FROM lock_queue WHERE node_id = ? ORDER BY priority DESC, id ASC",
        (node_id,),
    ).fetchall()
    for i, (aid,) in enumerate(rows):
        if aid == agent_id:
            return i
    return 0


def _dequeue_agent(node_id: str, agent_id: str, conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM lock_queue WHERE node_id = ? AND agent_id = ?", (node_id, agent_id)
    )
    conn.commit()


def _store_snapshot(
    node_id: str,
    file_path: str,
    func_name: str | None,
    project_root: str,
    caller_sources: list[str],
    conn: sqlite3.Connection,
) -> None:
    """Take and store an I/O snapshot at lock acquisition (skipped for external nodes)."""
    if conn.execute("SELECT 1 FROM snapshots WHERE node_id = ?", (node_id,)).fetchone():
        return  # already snapshotted from a prior lock acquisition
    kind_row = conn.execute("SELECT kind FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
    if kind_row and kind_row[0] == "external":
        return  # external nodes are not executed
    try:
        source_content = Path(file_path).read_text(encoding="utf-8")
    except OSError:
        return
    args, io_snap = capture_node_snapshot(file_path, func_name, project_root, caller_sources)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO snapshots "
            "(node_id, file_path, source_content, io_snapshot, io_args) "
            "VALUES (?, ?, ?, ?, ?)",
            (node_id, file_path, source_content,
             json.dumps(io_snap) if io_snap is not None else None,
             json.dumps(args)    if args    is not None else None),
        )


def _cascade_trigger(
    file_path: str,
    root_node: str,
    agent_id: str,
    agent_type: str | None,
    conn: sqlite3.Connection,
) -> list[str]:
    """
    Crawl reverse edges from file_path, acquire cascade locks on all dependent
    structural nodes (innermost / direct callers first), and record the cascade.
    Returns the list of cascade node_ids, empty if nothing to cascade.
    """
    direct = {r[0] for r in conn.execute(
        "SELECT from_file FROM edges WHERE to_file = ?", (file_path,)
    ).fetchall()}
    all_dep_files = crawl_subgraph(file_path, depth=2, conn=conn, direction="reverse") - {file_path}
    # Direct callers first (innermost), then transitive.
    ordered = sorted(all_dep_files, key=lambda f: (f not in direct, f))

    cascade_nodes: list[str] = []
    bloom = load_bloom(conn)
    with conn:
        for dep_file in ordered:
            rows = conn.execute(
                "SELECT node_id, file_path FROM nodes "
                "WHERE file_path = ? AND kind IN ('structural', 'external')",
                (dep_file,),
            ).fetchall()
            for nid, nfp in rows:
                existing = conn.execute(
                    "SELECT agent_id FROM locks WHERE node_id = ?", (nid,)
                ).fetchone()
                if existing and existing[0] != agent_id:
                    continue  # locked by another agent — skip, don't block cascade
                if not existing:
                    ttl = _compute_ttl(nid, conn)
                    conn.execute(
                        "INSERT OR IGNORE INTO locks "
                        "(node_id, file_path, agent_id, agent_type, expires_at) "
                        "VALUES (?, ?, ?, 'cascade', datetime('now', ? || ' seconds'))",
                        (nid, nfp, agent_id, f"+{ttl}"),
                    )
                    bloom.add(nid)
                cascade_nodes.append(nid)

    if cascade_nodes:
        save_bloom(conn, bloom)
        cascade_id = _uuid.uuid4().hex[:8]
        with conn:
            conn.execute(
                "INSERT INTO cascades "
                "(cascade_id, root_node, agent_id, cascade_nodes, completed_nodes) "
                "VALUES (?, ?, ?, ?, '[]')",
                (cascade_id, root_node, agent_id, json.dumps(cascade_nodes)),
            )

    return cascade_nodes


def _cascade_mark_complete(node_id: str, agent_id: str, conn: sqlite3.Connection) -> bool:
    """
    Mark node_id complete in any in-progress cascade owned by agent_id.
    Returns True if marking this node finishes the entire cascade.
    """
    rows = conn.execute(
        "SELECT cascade_id, cascade_nodes, completed_nodes FROM cascades "
        "WHERE agent_id = ? AND status = 'in_progress'",
        (agent_id,),
    ).fetchall()
    for cascade_id, cascade_json, completed_json in rows:
        cascade_nodes = json.loads(cascade_json)
        if node_id not in cascade_nodes:
            continue
        completed = json.loads(completed_json)
        if node_id not in completed:
            completed.append(node_id)
        with conn:
            conn.execute(
                "UPDATE cascades SET completed_nodes = ? WHERE cascade_id = ?",
                (json.dumps(completed), cascade_id),
            )
        if set(completed) >= set(cascade_nodes):
            with conn:
                conn.execute(
                    "UPDATE cascades SET status = 'complete' WHERE cascade_id = ?",
                    (cascade_id,),
                )
            return True
    return False


# ---------------------------------------------------------------------------
# Agent traversal recording
# ---------------------------------------------------------------------------

def _record_traversal(
    agent_id: str,
    node_ids: list[str],
    from_file: str,
    dep_files: set[str],
    conn: sqlite3.Connection,
) -> None:
    """
    Append touched nodes and traversed edges to the agent's traversal subgraph.
    node_ids: structural/external nodes the agent targeted in from_file.
    dep_files: files reached by the 1-hop forward crawl from from_file.
    Edges stored are the actual import edges the coordinator followed.
    """
    with conn:
        if node_ids:
            conn.executemany(
                "INSERT OR IGNORE INTO agent_traversal_nodes (agent_id, node_id) VALUES (?, ?)",
                [(agent_id, nid) for nid in node_ids],
            )
        for dep in dep_files:
            conn.execute(
                "INSERT OR IGNORE INTO agent_traversal_edges (agent_id, from_file, to_file) "
                "VALUES (?, ?, ?)",
                (agent_id, from_file, dep),
            )


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
    # Non-Python files have no AST graph — no conflict detection possible.
    if not _is_python(file_path):
        allow()

    project_root = find_project_root(file_path)

    # 1. Cold cache: ensure target is indexed before any node lookups.
    if not is_indexed(file_path, conn):
        index_file(file_path, project_root, conn)

    # 2. Identify which nodes this edit targets; filter to structural/external.
    target_node_ids = identify_target_nodes(file_path, tool_name, tool_input, conn)
    if target_node_ids:
        ph, vals = _in_clause(target_node_ids)
        structural_targets = [
            r[0] for r in conn.execute(
                f"SELECT node_id FROM nodes WHERE node_id IN ({ph}) AND kind IN ('structural', 'external')",
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

    # Record this agent's traversal: nodes targeted + edges followed.
    _record_traversal(agent_id, structural_targets, file_path, dep_files, conn)

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

    # Check structural/external nodes in direct dependencies.
    dep_conflicts: list[tuple[str, str]] = []
    if dep_files:
        ph, vals = _in_clause(dep_files)
        dep_structural = conn.execute(
            f"SELECT node_id FROM nodes WHERE file_path IN ({ph}) AND kind IN ('structural', 'external')",
            vals,
        ).fetchall()
        for (dep_nid,) in dep_structural:
            blocker = check_locked_by_other(dep_nid)
            if blocker:
                dep_conflicts.append((dep_nid, blocker))

    all_conflicts = target_conflicts + dep_conflicts
    if all_conflicts:
        # Check if any conflict is a cascade lock.
        cascade_detail = ""
        for nid, blocker in all_conflicts:
            lock_row = conn.execute(
                "SELECT agent_type FROM locks WHERE node_id = ?", (nid,)
            ).fetchone()
            if lock_row and lock_row[0] == "cascade":
                cascade_detail = f" (cascade refactor in progress by '{blocker}')"
                break
        # Queue this agent for each conflicting target node.
        for nid in structural_targets:
            _enqueue_agent(nid, agent_id, agent_type, conn)
        positions = [_queue_position(nid, agent_id, conn) for nid in structural_targets]
        pos_str = f"queue position {min(positions)}" if positions else "queued"
        details = "; ".join(f"'{nid}' held by '{b}'" for nid, b in all_conflicts)
        block(f"Lock conflict{cascade_detail} — {details}. {pos_str}. Replan.")

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
            ttl = _compute_ttl(nid, conn)
            try:
                conn.execute(
                    "INSERT INTO locks "
                    "(node_id, file_path, agent_id, agent_type, subgraph_hash, subgraph_nodes, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, datetime('now', ? || ' seconds'))",
                    (nid, file_path, agent_id, agent_type,
                     subgraph_hash, json.dumps(subgraph_node_ids), f"+{ttl}"),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT agent_id FROM locks WHERE node_id = ?", (nid,)
                ).fetchone()
                if row and row[0] != agent_id:
                    # Bloom false negative — another agent acquired between check and insert.
                    block(f"Node '{nid}' locked by '{row[0]}' (race). Replan.")
                # Same agent re-entering: refresh subgraph snapshot and TTL.
                conn.execute(
                    "UPDATE locks SET subgraph_hash = ?, subgraph_nodes = ?, "
                    "expires_at = datetime('now', ? || ' seconds') WHERE node_id = ?",
                    (subgraph_hash, json.dumps(subgraph_node_ids), f"+{ttl}", nid),
                )

    # 7. Update bloom and dequeue this agent (it's now the lock holder).
    for nid in structural_targets:
        bloom.add(nid)
        _dequeue_agent(nid, agent_id, conn)
    save_bloom(conn, bloom)

    # 8. Take I/O snapshots for contract validation at PostToolUse.
    caller_files = crawl_subgraph(file_path, depth=1, conn=conn, direction="reverse") - {file_path}
    caller_sources = []
    for cf in caller_files:
        try:
            caller_sources.append(Path(cf).read_text(encoding="utf-8"))
        except OSError:
            pass
    for nid in structural_targets:
        if _is_sentinel(nid):
            continue
        name_row = conn.execute("SELECT name FROM nodes WHERE node_id = ?", (nid,)).fetchone()
        _store_snapshot(nid, file_path, name_row[0] if name_row else None,
                        project_root, caller_sources, conn)

    allow()


# ---------------------------------------------------------------------------
# PostToolUse
# ---------------------------------------------------------------------------

def handle_release(
    file_path: str,
    agent_id: str,
    conn: sqlite3.Connection,
    agent_type: str | None = None,
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

    # Contract validation: re-execute each node and compare structure against snapshot.
    # A superset failure triggers the cascade flow (does not block the original edit).
    for row in rows:
        node_id, _, _ = row
        if _is_sentinel(node_id):
            continue
        snap_row = conn.execute(
            "SELECT io_snapshot, io_args FROM snapshots WHERE node_id = ?", (node_id,)
        ).fetchone()
        if not snap_row or not snap_row[0]:
            continue
        old_snap = json.loads(snap_row[0])
        args     = json.loads(snap_row[1]) if snap_row[1] else []
        name_row = conn.execute("SELECT name FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        if not name_row or not name_row[0]:
            continue
        new_snap = take_snapshot(file_path, name_row[0], args, project_root)
        if new_snap is None:
            continue
        if not is_superset(old_snap, new_snap):
            cascade_nodes = _cascade_trigger(file_path, node_id, agent_id, agent_type, conn)
            if cascade_nodes:
                names = [n.rsplit("::", 1)[-1] for n in cascade_nodes]
                revert_cmd = (
                    f"echo '{{\"agent_id\": \"{agent_id}\"}}' "
                    f"| python {Path(__file__).resolve()} --revert"
                )
                print(
                    f"\nI/O contract change detected on '{name_row[0]}' by '{agent_id}'.\n"
                    f"Dependents requiring update: {', '.join(names)}.\n"
                    "Cascade lock acquired. Proceed with updates, or revert all changes with:\n"
                    f"  {revert_cmd}",
                    file=sys.stderr,
                )

    # Release locks for this file.
    conn.execute(
        "DELETE FROM locks WHERE file_path = ? AND agent_id = ?",
        (file_path, agent_id),
    )
    conn.commit()
    for row in rows:
        bloom.remove(row[0])
    save_bloom(conn, bloom)

    # Cascade completion: mark each released node done; emit message if cascade finishes.
    for row in rows:
        node_id = row[0]
        if _is_sentinel(node_id):
            continue
        if _cascade_mark_complete(node_id, agent_id, conn):
            print(
                f"Cascade complete for agent '{agent_id}' — all dependent contracts verified.",
                file=sys.stderr,
            )

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
# Voluntary cascade revert
# ---------------------------------------------------------------------------

def handle_revert(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Restore all cascade nodes (and the root node) to their pre-lock snapshots,
    release all cascade locks, and mark the cascade reverted.

    Invoked explicitly by the agent via Bash:
        echo '{"agent_id": "..."}' | python coordinator.py --revert
    """
    agent_id = payload.get("agent_id") or payload.get("session_id", "unknown")

    rows = conn.execute(
        "SELECT cascade_id, root_node, cascade_nodes FROM cascades "
        "WHERE agent_id = ? AND status = 'in_progress'",
        (agent_id,),
    ).fetchall()

    if not rows:
        print(f"No active cascade for agent '{agent_id}'.", file=sys.stderr)
        sys.exit(0)

    bloom = load_bloom(conn)

    for cascade_id, root_node, cascade_nodes_json in rows:
        cascade_nodes = json.loads(cascade_nodes_json)
        all_nodes = [root_node] + [n for n in cascade_nodes if n != root_node]

        reverted_files: set[str] = set()
        for nid in all_nodes:
            snap = conn.execute(
                "SELECT file_path, source_content FROM snapshots WHERE node_id = ?",
                (nid,),
            ).fetchone()
            if not snap or not snap[1] or snap[0] in reverted_files:
                continue
            file_path, source = snap
            try:
                Path(file_path).write_text(source, encoding="utf-8")
                reverted_files.add(file_path)
                # Re-index so the cache reflects the reverted content.
                update_file(file_path, find_project_root(file_path), conn)
            except OSError as e:
                print(f"Revert write failed for '{file_path}': {e}", file=sys.stderr)

        # Release all cascade locks.
        with conn:
            for nid in all_nodes:
                conn.execute(
                    "DELETE FROM locks WHERE node_id = ? AND agent_id = ?",
                    (nid, agent_id),
                )
                bloom.remove(nid)
            conn.execute(
                "UPDATE cascades SET status = 'reverted' WHERE cascade_id = ?",
                (cascade_id,),
            )

        root_name = root_node.rsplit("::", 1)[-1]
        print(
            f"Cascade reverted — '{root_name}' and {len(cascade_nodes)} dependent(s) "
            "restored to pre-edit state.",
            file=sys.stderr,
        )

    save_bloom(conn, bloom)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _release_agent_locks(agent_id: str, conn: sqlite3.Connection) -> None:
    """Release all locks and queue entries held by agent_id (no cache update)."""
    locked = conn.execute(
        "SELECT node_id FROM locks WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    conn.execute("DELETE FROM locks WHERE agent_id = ?", (agent_id,))
    conn.execute("DELETE FROM lock_queue WHERE agent_id = ?", (agent_id,))
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
    if not Path(file_path).is_file() or not _is_python(file_path):
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


def handle_crawl_project(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Walk the project root recursively and index every .py file.
    Skips files that are already fresh. Ideal for small codebases where
    warming the full graph once at session start is faster than lazy per-file indexing.

    Invoke via:
        echo '{"session_id": "..."}' | python coordinator.py --crawl-project
    """
    cwd = payload.get("cwd", "") or str(Path.cwd())
    project_root = find_project_root(cwd)
    _SKIP_DIRS = frozenset({
        ".venv", "venv", ".env", "env", "site-packages",
        "__pycache__", ".git", "node_modules", ".tox", "build", "dist",
    })

    root_path = Path(project_root)
    indexed = skipped = errors = 0
    for py_file in sorted(root_path.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in py_file.parts):
            continue
        abs_path = str(py_file.resolve())
        try:
            if is_fresh(abs_path, conn):
                skipped += 1
            else:
                index_file(abs_path, project_root, conn)
                indexed += 1
        except Exception:
            errors += 1
    print(
        f"crawl-project: {indexed} indexed, {skipped} fresh (skipped), {errors} errors "
        f"in '{project_root}'",
        file=sys.stderr,
    )
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

    if   "--revert"          in args: mode = "revert"
    elif "--session-start"   in args: mode = "session_start"
    elif "--session-end"     in args: mode = "session_end"
    elif "--subagent-start"  in args: mode = "subagent_start"
    elif "--subagent-stop"   in args: mode = "subagent_stop"
    elif "--file-changed"    in args: mode = "file_changed"
    elif "--cwd-changed"     in args: mode = "cwd_changed"
    elif "--crawl-project"   in args: mode = "crawl_project"
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

    if mode == "revert":
        handle_revert(payload, conn)
    elif mode == "pretool":
        file_path, agent_id, agent_type, session_id, tool_name, tool_input = extract_context(payload)
        empty_path = str(Path("").resolve())
        if not file_path or file_path == empty_path:
            sys.exit(0)
        handle_pretool(file_path, agent_id, agent_type, tool_name, tool_input, conn)
    elif mode == "release":
        file_path, agent_id, agent_type, *_ = extract_context(payload)
        handle_release(file_path, agent_id, conn, agent_type)
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
    elif mode == "crawl_project":
        handle_crawl_project(payload, conn)
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
