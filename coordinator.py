#!/usr/bin/env python3
"""
coordinator.py — multi-agent subgraph lock coordinator, v1.

Reads hook context from stdin (JSON). Exits 0 (allow) or 2 (block).

Flags:
  (none)              PreToolUse         — conflict check, lock acquisition, subgraph snapshot
  --release           PostToolUse        — drift check, global cache update, lock release
  --failure           PostToolUseFailure — silent lock release, no cache update
  --crawl-project     manual / session-start — recursively index all .py files in project root
  --worktree-create   create a git worktree + branch for an agent, register in DB
  --worktree-push     merge agent branch → main, re-index changed files in canonical DB
  --worktree-pull     rebase agent worktree onto latest main, update base_commit in DB
  --worktree-status   refresh + report live status of all registered worktrees (JSON stdout)
  --worktree-log      query the worktree event log (JSON stdout); filter by agent/path/limit
  --worktree-remove   remove worktree + branch, release all agent locks

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

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid as _uuid
from pathlib import Path

try:
    import pygit2 as _pygit2
    _HAS_PYGIT2 = True
except ImportError:
    _HAS_PYGIT2 = False

from dep_graph import (
    compute_merkle_root,
    compute_merkle_root_from_node_ids,
    identify_target_nodes,
    index_file,
    is_fresh,
    crawl_subgraph,
    update_file,
    parse_file,
)
from contract import _UNRESOLVABLE, capture_node_snapshot, is_superset, take_snapshot

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
    subgraph_nodes       TEXT,
    subgraph_node_hashes TEXT,
    acquired_at          TEXT DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS worktrees (
    worktree_path    TEXT PRIMARY KEY,
    agent_id         TEXT,
    session_id       TEXT,
    branch_name      TEXT,
    base_commit      TEXT,
    current_commit   TEXT,
    commits_ahead    INTEGER DEFAULT 0,
    is_dirty         INTEGER DEFAULT 0,
    changed_files    TEXT DEFAULT '[]',
    last_status_at   TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS worktree_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event          TEXT NOT NULL,
    worktree_path  TEXT NOT NULL,
    agent_id       TEXT,
    branch_name    TEXT,
    from_commit    TEXT,
    to_commit      TEXT,
    detail         TEXT,
    occurred_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wt_events_agent ON worktree_events(agent_id);
CREATE INDEX IF NOT EXISTS idx_wt_events_path  ON worktree_events(worktree_path);

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

CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    pre_head      TEXT,
    project_root  TEXT,
    started_at    TEXT DEFAULT (datetime('now'))
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
    # Anchor DB to the main repo root so all per-worktree hook processes share one DB.
    try:
        cwd = str(Path.cwd())
        project_root = find_project_root(cwd)
        main_root = _main_repo_root(project_root)
    except Exception:
        main_root = str(Path.cwd())
    db_path = Path(main_root) / ".claude" / "coordinator.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
    # v2 → v3: add branch_name and base_commit to worktrees.
    wt_cols = {r[1] for r in conn.execute("PRAGMA table_info(worktrees)").fetchall()}
    if "branch_name" not in wt_cols:
        conn.execute("ALTER TABLE worktrees ADD COLUMN branch_name TEXT")
    if "base_commit" not in wt_cols:
        conn.execute("ALTER TABLE worktrees ADD COLUMN base_commit TEXT")
    # v3 → v4: add live status columns to worktrees.
    if "current_commit" not in wt_cols:
        conn.execute("ALTER TABLE worktrees ADD COLUMN current_commit TEXT")
    if "commits_ahead" not in wt_cols:
        conn.execute("ALTER TABLE worktrees ADD COLUMN commits_ahead INTEGER DEFAULT 0")
    if "is_dirty" not in wt_cols:
        conn.execute("ALTER TABLE worktrees ADD COLUMN is_dirty INTEGER DEFAULT 0")
    if "changed_files" not in wt_cols:
        conn.execute("ALTER TABLE worktrees ADD COLUMN changed_files TEXT DEFAULT '[]'")
    if "last_status_at" not in wt_cols:
        conn.execute("ALTER TABLE worktrees ADD COLUMN last_status_at TEXT")
    # v4 → v5: worktree event log.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS worktree_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            event          TEXT NOT NULL,
            worktree_path  TEXT NOT NULL,
            agent_id       TEXT,
            branch_name    TEXT,
            from_commit    TEXT,
            to_commit      TEXT,
            detail         TEXT,
            occurred_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_wt_events_agent ON worktree_events(agent_id);
        CREATE INDEX IF NOT EXISTS idx_wt_events_path  ON worktree_events(worktree_path);
    """)
    conn.commit()
    # v5 → v6: per-node hashes in locks for ownership-aware drift detection.
    lock_cols = {r[1] for r in conn.execute("PRAGMA table_info(locks)").fetchall()}
    if "subgraph_node_hashes" not in lock_cols:
        conn.execute("ALTER TABLE locks ADD COLUMN subgraph_node_hashes TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_python(file_path: str) -> bool:
    return Path(file_path).suffix == ".py"


# ---------------------------------------------------------------------------
# Git worktree helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


_main_repo_root_cache: dict[str, str] = {}


def _main_repo_root(worktree_path: str) -> str:
    """Return the root of the main (canonical) repo given any worktree path."""
    abs_wt = str(Path(worktree_path).resolve())
    if abs_wt in _main_repo_root_cache:
        return _main_repo_root_cache[abs_wt]

    if _HAS_PYGIT2:
        try:
            repo = _pygit2.Repository(abs_wt)
            git_dir = Path(repo.path.rstrip("/"))
            # For a linked worktree, git_dir is <main>/.git/worktrees/<name>/.
            # The 'commondir' file inside it points back to the shared .git dir.
            commondir_file = git_dir / "commondir"
            if commondir_file.exists():
                common = commondir_file.read_text().strip()
                if not Path(common).is_absolute():
                    common = str((git_dir / common).resolve())
                result = str(Path(common).parent)
            else:
                # Main repo (not a linked worktree) — git_dir is .git/ itself.
                result = str(git_dir.parent)
            _main_repo_root_cache[abs_wt] = result
            return result
        except Exception:
            pass

    _, common_dir, _ = _git(["rev-parse", "--git-common-dir"], cwd=worktree_path)
    git_path = Path(common_dir)
    if not git_path.is_absolute():
        git_path = (Path(worktree_path) / git_path).resolve()
    result = str(git_path.parent)
    _main_repo_root_cache[abs_wt] = result
    return result


def _worktree_to_canonical(file_path: str, conn: sqlite3.Connection) -> str | None:
    """
    If file_path lives inside a registered worktree, return the equivalent
    path in the main repo. Returns None if the file is already in the main repo.
    """
    abs_path = str(Path(file_path).resolve())
    for (wt_path,) in conn.execute("SELECT worktree_path FROM worktrees").fetchall():
        wt_abs = str(Path(wt_path).resolve())
        if abs_path.startswith(wt_abs + "/"):
            rel = abs_path[len(wt_abs):].lstrip("/")
            try:
                main_root = _main_repo_root(wt_path)
            except Exception:
                return None
            return str(Path(main_root) / rel)
    return None


def _refresh_worktree_status(worktree_path: str, conn: sqlite3.Connection) -> dict:
    """
    Query a worktree's live git state and persist it to the worktrees table.
    Returns the status dict (useful for printing without a second DB read).
    """
    row = conn.execute(
        "SELECT branch_name, base_commit FROM worktrees WHERE worktree_path = ?",
        (worktree_path,),
    ).fetchone()
    branch_name = row[0] if row else None
    base_commit  = row[1] if row else None

    # Current HEAD.
    _, current_commit, _ = _git(["rev-parse", "HEAD"], cwd=worktree_path)

    # How many commits ahead of main is this branch?
    try:
        main_root = _main_repo_root(worktree_path)
        _, ahead_str, _ = _git(
            ["rev-list", "--count", "main..HEAD"], cwd=worktree_path
        )
        commits_ahead = int(ahead_str) if ahead_str.isdigit() else 0
    except Exception:
        main_root = worktree_path
        commits_ahead = 0

    # Uncommitted changes (staged + unstaged).
    _, dirty_out, _ = _git(["status", "--porcelain"], cwd=worktree_path)
    is_dirty = 1 if dirty_out.strip() else 0

    # Files changed since base_commit (or all commits if no base recorded).
    if base_commit and base_commit != current_commit:
        _, diff_out, _ = _git(
            ["diff", "--name-only", base_commit, "HEAD"], cwd=worktree_path
        )
    elif base_commit == current_commit:
        # Only uncommitted changes relative to HEAD.
        _, diff_out, _ = _git(["diff", "--name-only", "HEAD"], cwd=worktree_path)
    else:
        diff_out = ""

    changed_files = [f for f in diff_out.splitlines() if f]

    # Include working-tree dirty files not yet committed.
    if dirty_out.strip():
        dirty_files = [
            line[3:].strip() for line in dirty_out.splitlines() if line.strip()
        ]
        changed_files = list(dict.fromkeys(changed_files + dirty_files))

    status = {
        "worktree_path": worktree_path,
        "branch_name": branch_name,
        "base_commit": base_commit,
        "current_commit": current_commit,
        "commits_ahead": commits_ahead,
        "is_dirty": bool(is_dirty),
        "changed_files": changed_files,
    }

    with conn:
        conn.execute(
            "UPDATE worktrees SET current_commit=?, commits_ahead=?, is_dirty=?, "
            "changed_files=?, last_status_at=datetime('now') WHERE worktree_path=?",
            (
                current_commit,
                commits_ahead,
                is_dirty,
                json.dumps(changed_files),
                worktree_path,
            ),
        )
    return status


def _log_worktree_event(
    event: str,
    worktree_path: str,
    conn: sqlite3.Connection,
    *,
    agent_id: str | None = None,
    branch_name: str | None = None,
    from_commit: str | None = None,
    to_commit: str | None = None,
    detail: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO worktree_events "
            "(event, worktree_path, agent_id, branch_name, from_commit, to_commit, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event, worktree_path, agent_id, branch_name, from_commit, to_commit, detail),
        )


def _get_worktree_for_file(file_path: str, conn: sqlite3.Connection) -> str | None:
    """Return the worktree_path if file_path lives inside a registered worktree, else None."""
    abs_path = str(Path(file_path).resolve())
    for (wt_path,) in conn.execute("SELECT worktree_path FROM worktrees").fetchall():
        wt_abs = str(Path(wt_path).resolve())
        if abs_path.startswith(wt_abs + "/"):
            return wt_path
    return None


def _expire_stale_locks(conn: sqlite3.Connection) -> None:
    """
    Scan for locks past their TTL, revert files from snapshots where available,
    and release the locks. Prevents crashed agents from permanently blocking nodes.
    Called at the start of each PreToolUse.
    """
    expired = conn.execute(
        "SELECT node_id, file_path, agent_id FROM locks "
        "WHERE expires_at IS NOT NULL AND expires_at < datetime('now')"
    ).fetchall()

    if not expired:
        return

    reverted_files: set[str] = set()

    for node_id, file_path, agent_id in expired:
        if file_path not in reverted_files:
            snap = conn.execute(
                "SELECT source_content FROM snapshots WHERE node_id = ?", (node_id,)
            ).fetchone()
            if snap and snap[0]:
                try:
                    Path(file_path).write_text(snap[0], encoding="utf-8")
                    reverted_files.add(file_path)
                    update_file(file_path, find_project_root(file_path), conn)
                    print(
                        f"[coordinator] TTL expired: reverted '{Path(file_path).name}' "
                        f"(was held by '{agent_id}')",
                        file=sys.stderr,
                    )
                except OSError as e:
                    print(f"[coordinator] TTL revert failed for '{file_path}': {e}", file=sys.stderr)

        conn.execute("DELETE FROM locks WHERE node_id = ?", (node_id,))

    conn.commit()
    print(
        f"[coordinator] TTL: released {len(expired)} expired lock(s), "
        f"reverted {len(reverted_files)} file(s)",
        file=sys.stderr,
    )


def _subgraph_stale(
    file_path: str,
    dep_files: set[str],
    wt_path: str,
    conn: sqlite3.Connection,
) -> tuple[list[str], list[str]]:
    """
    Compare the agent's worktree subgraph against the canonical DB using a
    two-phase approach:

    Fast path  — compute a combined hash of all canonical node hashes from the
                 DB and a combined hash of all worktree node hashes in one pass.
                 If they match, the subgraphs are identical: return [] immediately
                 without per-node SQLite queries.

    Slow path  — if the combined hashes differ, walk the already-loaded data to
                 identify which specific nodes are stale.

    Returns a list of stale node names (empty = views match, safe to proceed).
    """
    all_wt_files = {file_path} | dep_files

    # --- single-pass data collection ---
    # wt_to_canonical: worktree path → canonical abs path
    # canonical_db:    canonical abs path → {node_name: db_hash}
    wt_to_canonical: dict[str, str] = {}
    canonical_db: dict[str, dict[str, str]] = {}

    for wt_file in all_wt_files:
        canonical_path = _worktree_to_canonical(wt_file, conn)
        if not canonical_path:
            continue
        can_abs = str(Path(canonical_path).resolve())
        wt_to_canonical[wt_file] = can_abs
        db_rows = conn.execute(
            "SELECT node_id, content_hash FROM nodes WHERE file_path = ?",
            (can_abs,),
        ).fetchall()
        if db_rows:
            canonical_db[can_abs] = {r[0].split("::")[-1]: r[1] for r in db_rows}

    if not canonical_db:
        return [], []  # nothing indexed in canonical DB yet — not stale

    # Parse worktree files once; cache results for slow path.
    wt_parsed_cache: dict[str, dict[str, str]] = {}
    for wt_file in wt_to_canonical:
        try:
            wt_parsed_cache[wt_file] = {
                n.name: n.content_hash for n in parse_file(wt_file) if n.name
            }
        except Exception:
            wt_parsed_cache[wt_file] = {}

    # --- fast path: compare combined hashes ---
    db_tokens = sorted(
        f"{name}:{h}"
        for nodes in canonical_db.values()
        for name, h in nodes.items()
    )
    wt_tokens = sorted(
        f"{name}:{h}"
        for wt_file, parsed in wt_parsed_cache.items()
        if wt_file in wt_to_canonical
        for name, h in parsed.items()
    )
   
    if (
        hashlib.sha256("|".join(db_tokens).encode()).hexdigest()
        == hashlib.sha256("|".join(wt_tokens).encode()).hexdigest()
    ):
        return [], []  # fast path: subgraphs match

    # --- slow path: find which nodes and files are stale ---
    stale_nodes: list[str] = []
    stale_files: list[str] = []
    for wt_file, can_abs in wt_to_canonical.items():
        db_nodes = canonical_db.get(can_abs, {})
        wt_parsed = wt_parsed_cache.get(wt_file, {})
        file_stale = False
        for name, db_hash in db_nodes.items():
            if name in wt_parsed and wt_parsed[name] != db_hash:
                stale_nodes.append(f"{Path(wt_file).name}::{name}")
                file_stale = True
        if file_stale:
            stale_files.append(wt_file)

    return stale_nodes, stale_files


def _sync_from_main(
    wt_path: str,
    stale_wt_files: list[str],
    conn: sqlite3.Connection,
    agent_id: str | None = None,
) -> bool:
    """
    Copy stale files from the main repo filesystem into the agent's worktree.
    Replaces the old git-rebase-based auto-pull on the PreToolUse hot path —
    no subprocess forks, no rebase conflict risk.
    Returns True if all files were synced, False if any copy failed.
    """
    row = conn.execute(
        "SELECT agent_id, branch_name FROM worktrees WHERE worktree_path = ?",
        (wt_path,),
    ).fetchone()
    effective_agent = agent_id or (row[0] if row else None)
    branch_name = row[1] if row else None

    synced = 0
    for wt_file in stale_wt_files:
        canonical = _worktree_to_canonical(wt_file, conn)
        if not canonical or not Path(canonical).is_file():
            continue
        try:
            shutil.copy2(canonical, wt_file)
            synced += 1
        except OSError as e:
            print(f"[coordinator] sync failed '{Path(wt_file).name}': {e}", file=sys.stderr)
            _log_worktree_event(
                "pull_failed", wt_path, conn,
                agent_id=effective_agent, branch_name=branch_name,
                detail=f"shutil sync failed for '{Path(wt_file).name}': {e}",
            )
            return False

    _log_worktree_event(
        "pull", wt_path, conn,
        agent_id=effective_agent, branch_name=branch_name,
        detail=f"synced {synced} stale file(s) from main repo (no git rebase)",
    )
    return synced > 0


def _propagate_to_main(
    file_path: str,
    wt_path: str,
    agent_id: str,
    conn: sqlite3.Connection,
) -> None:
    """
    Called from PostToolUse (handle_release) when the edited file lives in a
    registered worktree.

    Copies the validated file directly to the equivalent path in the main repo
    working tree via shutil.copy2 — no git subprocess per edit.  Git compilation
    is deferred to SessionEnd, where all propagated changes are staged and
    squashed into one commit.  This eliminates the rebase-then-ff-merge per edit
    that previously required ~8 subprocess forks on the PostToolUse hot path.
    """
    canonical = _worktree_to_canonical(file_path, conn)
    if not canonical:
        return

    row = conn.execute(
        "SELECT branch_name FROM worktrees WHERE worktree_path = ?", (wt_path,)
    ).fetchone()
    branch_name = row[0] if row else None

    try:
        Path(canonical).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, canonical)
    except OSError as e:
        print(f"[coordinator] propagate failed '{Path(file_path).name}': {e}", file=sys.stderr)
        return

    _log_worktree_event(
        "push", wt_path, conn,
        agent_id=agent_id, branch_name=branch_name,
        detail=f"propagated '{Path(file_path).name}' to main repo (deferred git)",
    )
    print(
        f"[coordinator] propagated '{Path(file_path).name}' → main (git deferred to SessionEnd)",
        file=sys.stderr,
    )


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
    """Take a fresh I/O snapshot at each lock acquisition (skipped for external nodes)."""
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
            "INSERT OR REPLACE INTO snapshots "
            "(node_id, file_path, source_content, io_snapshot, io_args) "
            "VALUES (?, ?, ?, ?, ?)",
            (node_id, file_path, source_content,
             json.dumps(io_snap) if io_snap is not None else None,
             json.dumps([None if a is _UNRESOLVABLE else a for a in args]) if args is not None else None),
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
                    # Cascade locks have no TTL — the watchdog must not expire them
                    # mid-refactor. Released via SubagentStop, SessionEnd, or --revert.
                    conn.execute(
                        "INSERT OR IGNORE INTO locks "
                        "(node_id, file_path, agent_id, agent_type, expires_at) "
                        "VALUES (?, ?, ?, 'cascade', NULL)",
                        (nid, nfp, agent_id),
                    )
                cascade_nodes.append(nid)

    if cascade_nodes:
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

    # 0a. Evict any locks whose TTL has expired (crashed agent recovery).
    _expire_stale_locks(conn)

    # 0b. Worktree Merkle-root staleness check.
    #    Before any lock is acquired, verify that the agent's view of the target
    #    file and its 1-hop dependency subgraph matches the canonical DB state.
    #    If not: auto-rebase the worktree onto main and block so the agent replans
    #    with the updated code. This makes merge conflicts structurally impossible:
    #    the lock window covers read → edit → commit → push-to-main atomically.
    wt_path = _get_worktree_for_file(file_path, conn)
    if wt_path:
        # Quick 1-hop crawl to know which dep files to include in the check.
        _quick_dep_files = (
            crawl_subgraph(file_path, depth=1, conn=conn, direction="forward") - {file_path}
        )
        stale_nodes, stale_files = _subgraph_stale(file_path, _quick_dep_files, wt_path, conn)
        if stale_nodes:
            synced = _sync_from_main(wt_path, stale_files, conn, agent_id)
            if synced:
                block(
                    f"Worktree was stale on {stale_nodes} — "
                    f"synced from main repo. Replan with the updated state."
                )
            else:
                block(
                    f"Worktree stale on {stale_nodes} and sync from main failed. "
                    f"Check main repo state for '{wt_path}', then replan."
                )

    # 1. Cold cache: ensure target is indexed before any node lookups.
    # Guard: skip index_file for non-existent files (Write to a new file).
    # Without this guard, index_file raises FileNotFoundError, coordinator exits
    # non-zero (treated as "allow" by Claude Code), and no lock is ever acquired —
    # every subsequent agent walks through silently on the same new file.
    if not is_indexed(file_path, conn) and Path(file_path).is_file():
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

    #TODO: align with README spec ~lines 265-266
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

    # 4. Conflict check — load the full locks table into a dict once.
    #    Eliminates the lost-update race in the bloom load/modify/save cycle.
    current_locks: dict[str, str] = dict(conn.execute(
        "SELECT node_id, agent_id FROM locks"
    ).fetchall())

    def check_locked_by_other(node_id: str) -> str | None:
        holder = current_locks.get(node_id)
        return holder if holder and holder != agent_id else None

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
    # Per-node hashes for ownership-aware drift detection (v6).
    if subgraph_node_ids:
        ph_h, vals_h = _in_clause(subgraph_node_ids)
        subgraph_node_hashes: dict[str, str] = {
            r[0]: r[1] for r in conn.execute(
                f"SELECT node_id, content_hash FROM nodes WHERE node_id IN ({ph_h})", vals_h
            ).fetchall()
        }
    else:
        subgraph_node_hashes = {}

    # 6. Acquire locks — single transaction, all-or-nothing.
    with conn:
        for nid in structural_targets:
            ttl = _compute_ttl(nid, conn)
            try:
                conn.execute(
                    "INSERT INTO locks "
                    "(node_id, file_path, agent_id, agent_type, subgraph_hash, subgraph_nodes, subgraph_node_hashes, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', ? || ' seconds'))",
                    (nid, file_path, agent_id, agent_type,
                     subgraph_hash, json.dumps(subgraph_node_ids),
                     json.dumps(subgraph_node_hashes), f"+{ttl}"),
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
                    "subgraph_node_hashes = ?, "
                    "expires_at = datetime('now', ? || ' seconds') WHERE node_id = ?",
                    (subgraph_hash, json.dumps(subgraph_node_ids),
                     json.dumps(subgraph_node_hashes), f"+{ttl}", nid),
                )

    # 7. Dequeue this agent (it's now the lock holder).
    for nid in structural_targets:
        _dequeue_agent(nid, agent_id, conn)

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
    project_root = find_project_root(file_path)

    rows = conn.execute(
        "SELECT node_id, subgraph_hash, subgraph_nodes, subgraph_node_hashes FROM locks "
        "WHERE file_path = ? AND agent_id = ?",
        (file_path, agent_id),
    ).fetchall()

    if not rows:
        # Lockless edit (benign node — imports, comments, etc.): update cache so node
        # hashes, line ranges, and edges reflect the new file state.
        update_file(file_path, project_root, conn)
        sys.exit(0)

    # Ownership-aware drift check — must run before update_file mutates DB hashes.
    # Exclude nodes this agent wrote itself (via agent_traversal) to avoid false positives
    # from the agent's own parallel-batch edits.
    agent_traversal: set[str] = {r[0] for r in conn.execute(
        "SELECT node_id FROM agent_traversal_nodes WHERE agent_id = ?", (agent_id,)
    ).fetchall()}

    for row in rows:
        node_id, stored_hash, subgraph_nodes_json, subgraph_node_hashes_json = row
        if _is_sentinel(node_id):
            continue

        stored_node_hashes: dict[str, str] = (
            json.loads(subgraph_node_hashes_json) if subgraph_node_hashes_json else {}
        )

        if not stored_node_hashes:
            # Fallback for lock rows predating v6 (no per-node hashes stored).
            stored_nodes = json.loads(subgraph_nodes_json) if subgraph_nodes_json else []
            recomputed   = compute_merkle_root_from_node_ids(stored_nodes, conn)
            if recomputed != stored_hash:
                #TODO: implement rollback
                conn.execute(
                    "DELETE FROM locks WHERE file_path = ? AND agent_id = ?",
                    (file_path, agent_id),
                )
                conn.commit()
                print(
                    f"Subgraph drifted for '{node_id}': a dependency was mutated during the edit "
                    "window. Edit may be built on stale state. Replan against current dependencies.",
                    file=sys.stderr,
                )
                sys.exit(2)
            continue

        # Per-node comparison: skip nodes this agent wrote itself.
        drifted: str | None = None
        for nid, stored_h in stored_node_hashes.items():
            if nid in agent_traversal or _is_sentinel(nid):
                continue
            curr = conn.execute(
                "SELECT content_hash FROM nodes WHERE node_id = ?", (nid,)
            ).fetchone()
            if curr and curr[0] != stored_h:
                drifted = nid
                break

        if drifted:
            #TODO: implement rollback
            conn.execute(
                "DELETE FROM locks WHERE file_path = ? AND agent_id = ?",
                (file_path, agent_id),
            )
            conn.commit()
            print(
                f"Subgraph drifted for '{drifted}': a dependency was mutated during the edit "
                "window. Edit may be built on stale state. Replan against current dependencies.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Subgraph is clean — update global cache.
    update_file(file_path, project_root, conn)

    # Worktree auto-push: propagate the validated edit to the main repo filesystem.
    # Git compilation is deferred to SessionEnd — no subprocess forks here.
    wt = _get_worktree_for_file(file_path, conn)
    if wt:
        _propagate_to_main(file_path, wt, agent_id, conn)

    # Contract validation: re-execute each node and compare structure against snapshot.
    # A superset failure triggers the cascade flow (does not block the original edit).
    cascade_triggered = False
    for row in rows:
        node_id, _, _, _ = row
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
                cascade_triggered = True
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

    # Delete snapshots on clean release so TTL/watchdog reverts always restore to the
    # pre-this-lock state, never older session state.
    # Exception: keep snapshots when a cascade was triggered — --revert needs them.
    if not cascade_triggered:
        for row in rows:
            node_id = row[0]
            if not _is_sentinel(node_id):
                conn.execute("DELETE FROM snapshots WHERE node_id = ?", (node_id,))
        conn.commit()

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
    conn.execute(
        "DELETE FROM locks WHERE file_path = ? AND agent_id = ?",
        (file_path, agent_id),
    )
    conn.commit()
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

    sys.exit(0)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _release_agent_locks(agent_id: str, conn: sqlite3.Connection) -> None:
    """Release all locks and queue entries held by agent_id (no cache update)."""
    conn.execute("DELETE FROM locks WHERE agent_id = ?", (agent_id,))
    conn.execute("DELETE FROM lock_queue WHERE agent_id = ?", (agent_id,))
    conn.commit()


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

def handle_session_start(payload: dict, conn: sqlite3.Connection) -> None:
    session_id   = payload.get("session_id", "unknown")
    project_root = str(Path.cwd())
    _, pre_head, _ = _git(["rev-parse", "HEAD"], cwd=project_root)
    if pre_head:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, pre_head, project_root) "
                "VALUES (?, ?, ?)",
                (session_id, pre_head, project_root),
            )
        print(
            f"[coordinator] session '{session_id[:8]}' started "
            f"(main HEAD: {pre_head[:8]})",
            file=sys.stderr,
        )
    sys.exit(0)


def handle_session_end(session_id: str, conn: sqlite3.Connection) -> None:
    # Compile all shutil-propagated edits into one clean commit on main —
    # but only if no agent locks are currently held (i.e. it's a true session end,
    # not a mid-session /compact). If agents are still active, skip so we don't
    # commit in-flight work.
    #
    # Two cases handled:
    #   (a) New flow — edits were propagated via shutil.copy2, working tree is dirty,
    #       no intermediate commits. Stage everything and commit once.
    #   (b) Legacy flow — micro-commits accumulated (old sessions or mixed state).
    #       Squash them with reset --soft, then commit as in case (a).
    #
    # NOTE: reset --soft rewrites local history. Safe for local-only workflow;
    # do not add automated remote pushes mid-session without revisiting this.
    active_locks = conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0]
    if active_locks == 0:
        sess = conn.execute(
            "SELECT pre_head, project_root FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if sess and sess[0] and sess[1]:
            pre_head, project_root = sess[0], sess[1]
            _, count_str, _ = _git(
                ["rev-list", "--count", f"{pre_head}..HEAD"], cwd=project_root
            )
            count = int(count_str.strip()) if count_str.strip().isdigit() else 0
            if count > 1:
                # Legacy: squash intermediate micro-commits back to baseline.
                _git(["reset", "--soft", pre_head], cwd=project_root)
            # Commit any staged/dirty files (shutil-propagated edits land here).
            _, dirty_out, _ = _git(["status", "--porcelain"], cwd=project_root)
            if dirty_out.strip():
                _git(["add", "."], cwd=project_root)
                label = f" — {count} agent edits" if count > 1 else ""
                _git(
                    ["commit", "-m",
                     f"coordinator: session {session_id[:8]}{label}"],
                    cwd=project_root,
                )
                print(
                    f"[coordinator] session commit: {count} edit(s) squashed → 1 on main",
                    file=sys.stderr,
                )
        with conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    conn.execute("DELETE FROM locks")
    conn.execute("DELETE FROM agents WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    sys.exit(0)


def handle_subagent_start(payload: dict, conn: sqlite3.Connection) -> None:
    agent_id   = payload.get("agent_id") or payload.get("session_id", "unknown")
    agent_type = payload.get("agent_type")
    session_id = payload.get("session_id", "")
    # Worktree registration is handled by the WorktreeCreate hook; SubagentStart
    # payload only carries agent_id, agent_type, session_id.
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO agents (agent_id, agent_type, session_id) VALUES (?, ?, ?)",
            (agent_id, agent_type, session_id),
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
    event = payload.get("event", "change")  # 'change' | 'add' | 'unlink'
    project_root = find_project_root(file_path)
    if event == "unlink":
        # File deleted — purge its nodes/edges and re-index dependents so the
        # graph doesn't carry stale edges pointing to a gone file.
        update_file(file_path, project_root, conn)
    else:
        _warm_from_file(file_path, depth=2, project_root=project_root, conn=conn)
    sys.exit(0)


def handle_cwd_changed(payload: dict, conn: sqlite3.Connection) -> None:
    # Real payload field is `new_cwd`; fall back to `cwd` for manual invocations.
    cwd = payload.get("new_cwd") or payload.get("cwd", "")
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


def handle_worktree_log(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Print the worktree event log as JSON to stdout.

    Payload (all optional):
        worktree_path  — filter to a single worktree
        agent_id       — filter to a single agent
        limit          — max rows to return (default 100)

    Invoke via:
        echo '{}' | python coordinator.py --worktree-log
        echo '{"agent_id": "agent-1", "limit": 20}' | python coordinator.py --worktree-log
    """
    wt_filter    = payload.get("worktree_path", "")
    agent_filter = payload.get("agent_id", "")
    limit        = int(payload.get("limit", 100))

    where, params = [], []
    if wt_filter:
        where.append("worktree_path = ?")
        params.append(wt_filter)
    if agent_filter:
        where.append("agent_id = ?")
        params.append(agent_filter)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    rows = conn.execute(
        f"SELECT id, event, worktree_path, agent_id, branch_name, "
        f"from_commit, to_commit, detail, occurred_at "
        f"FROM worktree_events {clause} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()

    events = [
        {
            "id": r[0],
            "event": r[1],
            "worktree_path": r[2],
            "agent_id": r[3],
            "branch_name": r[4],
            "from_commit": r[5][:8] if r[5] else None,
            "to_commit": r[6][:8] if r[6] else None,
            "detail": r[7],
            "occurred_at": r[8],
        }
        for r in rows
    ]
    print(json.dumps(events, indent=2), flush=True)
    sys.exit(0)


def handle_worktree_status(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Refresh and report the live status of every registered worktree (or one
    specific worktree if worktree_path is given in the payload).

    Output is JSON to stdout; progress/errors go to stderr.

    Invoke via:
        echo '{}' | python coordinator.py --worktree-status
        echo '{"worktree_path": "/tmp/wt-agent-1"}' | python coordinator.py --worktree-status
    """
    target = payload.get("worktree_path", "")
    if target:
        rows = conn.execute(
            "SELECT worktree_path FROM worktrees WHERE worktree_path = ?", (target,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT worktree_path FROM worktrees").fetchall()

    if not rows:
        print(json.dumps([]), flush=True)
        sys.exit(0)

    results = []
    for (wt_path,) in rows:
        if not Path(wt_path).is_dir():
            results.append({
                "worktree_path": wt_path,
                "error": "directory not found",
            })
            continue
        try:
            status = _refresh_worktree_status(wt_path, conn)
            results.append(status)
        except Exception as exc:
            results.append({"worktree_path": wt_path, "error": str(exc)})

    print(json.dumps(results, indent=2), flush=True)
    sys.exit(0)


def handle_worktree_create_hook(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Fired automatically by the WorktreeCreate hook when Claude Code creates a
    worktree. The payload has `name` (worktree name) but NOT the path — we
    resolve the path by scanning `git worktree list --porcelain` and matching
    on the directory name or branch name.
    """
    name       = payload.get("name", "")
    agent_id   = payload.get("agent_id") or payload.get("session_id", "unknown")
    session_id = payload.get("session_id", "")

    if not name:
        sys.exit(0)

    try:
        main_root = _main_repo_root(str(Path.cwd()))
    except Exception:
        sys.exit(0)

    _, wt_list, _ = _git(["worktree", "list", "--porcelain"], cwd=main_root)

    # Parse porcelain blocks: each block is separated by a blank line.
    # Fields: "worktree <path>", "HEAD <hash>", "branch refs/heads/<branch>"
    worktree_path: str | None = None
    branch_name:   str | None = None
    current_path:  str | None = None
    current_branch: str | None = None

    for line in wt_list.splitlines():
        if line.startswith("worktree "):
            current_path   = line[len("worktree "):]
            current_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):]               # refs/heads/<branch>
            current_branch = ref.rsplit("/", 1)[-1]
            # Match if the name equals the branch name or the worktree dir name.
            if name in (current_branch, Path(current_path).name if current_path else ""):
                worktree_path = current_path
                branch_name   = current_branch
                break

    if not worktree_path or not Path(worktree_path).is_dir():
        sys.exit(0)

    _, base_commit, _ = _git(["rev-parse", "HEAD"], cwd=worktree_path)

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO worktrees "
            "(worktree_path, agent_id, session_id, branch_name, base_commit) "
            "VALUES (?, ?, ?, ?, ?)",
            (worktree_path, agent_id, session_id, branch_name, base_commit),
        )
    _refresh_worktree_status(worktree_path, conn)
    _log_worktree_event(
        "create", worktree_path, conn,
        agent_id=agent_id, branch_name=branch_name,
        to_commit=base_commit,
        detail=f"auto-registered via WorktreeCreate hook (name={name})",
    )
    print(
        f"[coordinator] WorktreeCreate: registered '{branch_name}' at '{worktree_path}'",
        file=sys.stderr,
    )
    sys.exit(0)


def handle_worktree_create(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Create a git worktree for an agent and register it in the DB.

    Payload:
        worktree_path  — absolute path where the worktree should be created
        agent_id       — agent identifier (used as the branch name)
        session_id     — session identifier
        branch_name    — optional explicit branch name (default: agent_id)

    Invoke via:
        echo '{"agent_id": "agent-1", "worktree_path": "/tmp/wt-agent-1"}' \\
            | python coordinator.py --worktree-create
    """
    worktree_path = payload.get("worktree_path", "")
    if not worktree_path:
        sys.exit(0)
    agent_id    = payload.get("agent_id") or payload.get("session_id", "unknown")
    session_id  = payload.get("session_id", "")
    branch_name = payload.get("branch_name") or f"agent/{agent_id}"

    # Resolve the main repo root from the current working directory.
    try:
        main_root = _main_repo_root(str(Path.cwd()))
    except Exception:
        print(f"worktree-create: could not find git root", file=sys.stderr)
        sys.exit(1)

    # Create the worktree + branch.
    rc, _, err = _git(
        ["worktree", "add", worktree_path, "-b", branch_name],
        cwd=main_root,
    )
    if rc != 0:
        # Branch may already exist (idempotent re-registration).
        rc2, _, err2 = _git(
            ["worktree", "add", worktree_path, branch_name],
            cwd=main_root,
        )
        if rc2 != 0:
            print(f"worktree-create: git error: {err or err2}", file=sys.stderr)
            sys.exit(1)

    # Record the HEAD commit so staleness checks have a baseline.
    _, base_commit, _ = _git(["rev-parse", "HEAD"], cwd=worktree_path)

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO worktrees "
            "(worktree_path, agent_id, session_id, branch_name, base_commit) "
            "VALUES (?, ?, ?, ?, ?)",
            (worktree_path, agent_id, session_id, branch_name, base_commit),
        )
    _refresh_worktree_status(worktree_path, conn)
    _log_worktree_event(
        "create", worktree_path, conn,
        agent_id=agent_id, branch_name=branch_name,
        to_commit=base_commit,
        detail=f"worktree created at '{worktree_path}'",
    )
    print(
        f"worktree-create: '{branch_name}' at '{worktree_path}' (base {base_commit[:8]})",
        file=sys.stderr,
    )
    sys.exit(0)


def handle_worktree_push(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Merge an agent's worktree branch into main and re-index changed files in
    the canonical DB so subsequent agents see the updated state.

    Payload:
        worktree_path  — path to the agent's worktree
        agent_id       — agent identifier

    Invoke via:
        echo '{"agent_id": "agent-1", "worktree_path": "/tmp/wt-agent-1"}' \\
            | python coordinator.py --worktree-push
    """
    worktree_path = payload.get("worktree_path", "")
    agent_id      = payload.get("agent_id") or payload.get("session_id", "unknown")
    if not worktree_path:
        print("worktree-push: worktree_path required", file=sys.stderr)
        sys.exit(1)

    row = conn.execute(
        "SELECT branch_name FROM worktrees WHERE worktree_path = ?", (worktree_path,)
    ).fetchone()
    branch_name = row[0] if row else None

    try:
        main_root = _main_repo_root(worktree_path)
    except Exception:
        print("worktree-push: could not resolve main repo root", file=sys.stderr)
        sys.exit(1)

    if not branch_name:
        print("worktree-push: worktree not registered in coordinator DB", file=sys.stderr)
        sys.exit(1)

    # Record main HEAD before the merge so we can log from→to.
    _, pre_merge_head, _ = _git(["rev-parse", "HEAD"], cwd=main_root)

    # Merge the agent branch into main with a merge commit (--no-ff preserves history).
    rc, _, err = _git(
        ["merge", branch_name, "--no-ff", "-m",
         f"coordinator: merge {branch_name} ({agent_id})"],
        cwd=main_root,
    )
    if rc != 0:
        _log_worktree_event(
            "push_failed", worktree_path, conn,
            agent_id=agent_id, branch_name=branch_name,
            from_commit=pre_merge_head,
            detail=f"merge conflict: {err[:200]}",
        )
        print(f"worktree-push: merge failed — {err}. Resolve conflicts then retry.", file=sys.stderr)
        sys.exit(1)

    # Re-index files that changed in the merge to update the canonical DB.
    _, diff_output, _ = _git(
        ["diff", "--name-only", "HEAD~1", "HEAD"],
        cwd=main_root,
    )
    project_root = main_root
    indexed = 0
    for rel_path in diff_output.splitlines():
        if not rel_path.endswith(".py"):
            continue
        abs_path = str(Path(main_root) / rel_path)
        if Path(abs_path).is_file():
            try:
                update_file(abs_path, project_root, conn)
                indexed += 1
            except Exception:
                pass

    # Update base_commit in DB to reflect the new merge HEAD.
    _, new_head, _ = _git(["rev-parse", "HEAD"], cwd=main_root)
    with conn:
        conn.execute(
            "UPDATE worktrees SET base_commit = ? WHERE worktree_path = ?",
            (new_head, worktree_path),
        )

    _refresh_worktree_status(worktree_path, conn)
    _log_worktree_event(
        "push", worktree_path, conn,
        agent_id=agent_id, branch_name=branch_name,
        from_commit=pre_merge_head, to_commit=new_head,
        detail=f"merged into main; re-indexed {indexed} file(s)",
    )
    print(
        f"worktree-push: merged '{branch_name}' → main, re-indexed {indexed} file(s)",
        file=sys.stderr,
    )
    sys.exit(0)


def handle_worktree_pull(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Rebase an agent's worktree branch onto the latest main so the agent's view
    is current before resuming work.

    Payload:
        worktree_path  — path to the agent's worktree
        agent_id       — agent identifier

    Invoke via:
        echo '{"agent_id": "agent-1", "worktree_path": "/tmp/wt-agent-1"}' \\
            | python coordinator.py --worktree-pull
    """
    worktree_path = payload.get("worktree_path", "")
    if not worktree_path:
        print("worktree-pull: worktree_path required", file=sys.stderr)
        sys.exit(1)

    try:
        main_root = _main_repo_root(worktree_path)
    except Exception:
        print("worktree-pull: could not resolve main repo root", file=sys.stderr)
        sys.exit(1)

    _, pre_rebase_head, _ = _git(["rev-parse", "HEAD"], cwd=worktree_path)
    row = conn.execute(
        "SELECT agent_id, branch_name FROM worktrees WHERE worktree_path = ?",
        (worktree_path,),
    ).fetchone()
    agent_id    = row[0] if row else None
    branch_name = row[1] if row else None

    # Rebase the agent's branch onto the current main HEAD.
    rc, _, err = _git(["rebase", "main"], cwd=worktree_path)
    if rc != 0:
        _log_worktree_event(
            "pull_failed", worktree_path, conn,
            agent_id=agent_id, branch_name=branch_name,
            from_commit=pre_rebase_head,
            detail=f"rebase conflict: {err[:200]}",
        )
        print(
            f"worktree-pull: rebase failed — {err}. "
            "Resolve conflicts, then run 'git rebase --continue' in the worktree.",
            file=sys.stderr,
        )
        sys.exit(1)

    _, new_base, _ = _git(["rev-parse", "HEAD"], cwd=worktree_path)
    with conn:
        conn.execute(
            "UPDATE worktrees SET base_commit = ? WHERE worktree_path = ?",
            (new_base, worktree_path),
        )

    _refresh_worktree_status(worktree_path, conn)
    _log_worktree_event(
        "pull", worktree_path, conn,
        agent_id=agent_id, branch_name=branch_name,
        from_commit=pre_rebase_head, to_commit=new_base,
        detail="rebased onto main",
    )
    print(f"worktree-pull: rebased onto main (new HEAD {new_base[:8]})", file=sys.stderr)
    sys.exit(0)


def handle_worktree_remove(payload: dict, conn: sqlite3.Connection) -> None:
    """
    Remove an agent's worktree and its branch, release all held locks.

    Payload:
        worktree_path  — path to the agent's worktree
        delete_branch  — if true (default), delete the agent branch after removal

    Invoke via:
        echo '{"worktree_path": "/tmp/wt-agent-1"}' \\
            | python coordinator.py --worktree-remove
    """
    worktree_path = payload.get("worktree_path", "")
    delete_branch = payload.get("delete_branch", True)
    if not worktree_path:
        sys.exit(0)

    row = conn.execute(
        "SELECT agent_id, branch_name FROM worktrees WHERE worktree_path = ?",
        (worktree_path,),
    ).fetchone()

    if row:
        agent_id, branch_name = row
        _release_agent_locks(agent_id, conn)
    else:
        branch_name = None

    try:
        main_root = _main_repo_root(worktree_path)
    except Exception:
        main_root = None

    if main_root:
        _git(["worktree", "remove", "--force", worktree_path], cwd=main_root)
        if delete_branch and branch_name:
            _git(["branch", "-D", branch_name], cwd=main_root)

    _log_worktree_event(
        "remove", worktree_path, conn,
        agent_id=agent_id if row else None,
        branch_name=branch_name,
        detail=f"delete_branch={delete_branch}",
    )
    with conn:
        conn.execute("DELETE FROM worktrees WHERE worktree_path = ?", (worktree_path,))

    print(f"worktree-remove: removed '{worktree_path}'", file=sys.stderr)
    sys.exit(0)


def handle_post_tool_batch(payload: dict, conn: sqlite3.Connection) -> None:
    """
    NOTE: PostToolBatch is NOT a registerable hook event in the current Claude Code CLI.
    The settings.json schema validator rejects it with "Not a recognized hook event".
    This handler must be invoked manually if needed. Fallback: per-node drift is also
    caught at each individual PostToolUse in handle_release.

    Fires after a parallel batch of tool calls resolves, before the next model call.
    Re-validates subgraph snapshots held by this agent using ownership-aware comparison:
    hash deltas on nodes written by this agent in the same batch are excluded so the
    agent's own parallel edits don't false-positive as drift.
    """
    agent_id = payload.get("agent_id") or payload.get("session_id", "unknown")

    rows = conn.execute(
        "SELECT node_id, file_path, subgraph_hash, subgraph_nodes, subgraph_node_hashes FROM locks "
        "WHERE agent_id = ? AND subgraph_hash IS NOT NULL",
        (agent_id,),
    ).fetchall()

    agent_traversal: set[str] = {r[0] for r in conn.execute(
        "SELECT node_id FROM agent_traversal_nodes WHERE agent_id = ?", (agent_id,)
    ).fetchall()}

    drift_nodes: list[str] = []
    for node_id, _file_path, stored_hash, subgraph_nodes_json, subgraph_node_hashes_json in rows:
        if _is_sentinel(node_id):
            continue

        stored_node_hashes: dict[str, str] = (
            json.loads(subgraph_node_hashes_json) if subgraph_node_hashes_json else {}
        )

        if not stored_node_hashes:
            # Fallback to Merkle root comparison for pre-v6 lock rows.
            stored_nodes = json.loads(subgraph_nodes_json) if subgraph_nodes_json else []
            if not stored_nodes:
                continue
            recomputed = compute_merkle_root_from_node_ids(stored_nodes, conn)
            if recomputed != stored_hash:
                drift_nodes.append(node_id.rsplit("::", 1)[-1])
            continue

        # Per-node comparison: skip nodes this agent wrote itself.
        for nid, stored_h in stored_node_hashes.items():
            if nid in agent_traversal or _is_sentinel(nid):
                continue
            curr = conn.execute(
                "SELECT content_hash FROM nodes WHERE node_id = ?", (nid,)
            ).fetchone()
            if curr and curr[0] != stored_h:
                drift_nodes.append(node_id.rsplit("::", 1)[-1])
                break

    if drift_nodes:
        print(
            f"[coordinator] PostToolBatch: subgraph drift detected on "
            f"{drift_nodes} for '{agent_id}'. "
            "A dependency shifted during the batch. Replan against current state.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


def handle_pre_compact(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = set(sys.argv[1:])

    if   "--revert"               in args: mode = "revert"
    elif "--session-start"        in args: mode = "session_start"
    elif "--session-end"          in args: mode = "session_end"
    elif "--subagent-start"       in args: mode = "subagent_start"
    elif "--subagent-stop"        in args: mode = "subagent_stop"
    elif "--file-changed"         in args: mode = "file_changed"
    elif "--cwd-changed"          in args: mode = "cwd_changed"
    elif "--crawl-project"        in args: mode = "crawl_project"
    elif "--worktree-create-hook" in args: mode = "worktree_create_hook"
    elif "--worktree-create"      in args: mode = "worktree_create"
    elif "--worktree-push"        in args: mode = "worktree_push"
    elif "--worktree-pull"        in args: mode = "worktree_pull"
    elif "--worktree-status"      in args: mode = "worktree_status"
    elif "--worktree-log"         in args: mode = "worktree_log"
    elif "--worktree-remove"      in args: mode = "worktree_remove"
    elif "--post-tool-batch"      in args: mode = "post_tool_batch"
    elif "--pre-compact"          in args: mode = "pre_compact"
    elif "--release"              in args: mode = "release"
    elif "--failure"              in args: mode = "failure"
    else:                                  mode = "pretool"

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

    try:
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
        elif mode == "worktree_create_hook":
            handle_worktree_create_hook(payload, conn)
        elif mode == "worktree_create":
            handle_worktree_create(payload, conn)
        elif mode == "worktree_push":
            handle_worktree_push(payload, conn)
        elif mode == "worktree_pull":
            handle_worktree_pull(payload, conn)
        elif mode == "worktree_status":
            handle_worktree_status(payload, conn)
        elif mode == "worktree_log":
            handle_worktree_log(payload, conn)
        elif mode == "worktree_remove":
            handle_worktree_remove(payload, conn)
        elif mode == "post_tool_batch":
            handle_post_tool_batch(payload, conn)
        elif mode == "pre_compact":
            handle_pre_compact(conn)
    except SystemExit:
        raise
    except Exception as exc:
        if mode == "pretool":
            block(f"Coordinator error: {exc}")
        sys.exit(0)


if __name__ == "__main__":
    main()
