#!/usr/bin/env python3
"""
dep_graph.py — module-level dependency graph construction for the CBS coordinator.

Nodes are module-level AST constructs (functions, classes, imports, bare statements).
Edges are cross-file import dependencies within the project.
Hashes are SHA256 of node source content, not mtimes.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lomitus.contract import EXTERNAL_CALL_PREFIXES

SCHEMA = """
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

CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_file);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges(to_file);
"""

# TODO: implement effect typing for nodes, i.e pure,io,netowkr,db.. these should be propagated transitively
# if a pure function calls an io function, then it's io.
@dataclass(frozen=True)
class Node:
    node_id: str
    file_path: str
    name: str | None
    kind: Literal["structural", "external", "benign"]
    line_start: int
    line_end: int
    content_hash: str


def _call_root(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_root(node.value)
    return None

# TODO: refacor this to be durable, this should answer something closer to "does the call graph of this specific function reach a node with observable side effects at execution time."
def _has_external_calls(stmt: ast.stmt) -> bool:
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call):
            root = _call_root(node.func)
            if root and root in EXTERNAL_CALL_PREFIXES: #TODO: absolutely fucking not, this is not scalable in the slightest
                return True
    return False


def _classify(stmt: ast.stmt) -> Literal["structural", "external", "benign"]:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return "external" if _has_external_calls(stmt) else "structural"
    return "benign"


def _name(stmt: ast.stmt, class_name: str | None = None) -> str | None:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"{class_name}.{stmt.name}" if class_name else stmt.name
    if isinstance(stmt, ast.ClassDef):
        return stmt.name
    return None


def _node_id(file_path: str, stmt: ast.stmt, class_name: str | None = None) -> str:
    label = _name(stmt, class_name) or str(stmt.lineno)
    return f"{file_path}::{label}"


def _all_stmts(tree: ast.Module) -> list[tuple[ast.stmt, str | None]]:
    """
    Yield (stmt, class_name) for every indexable statement in a module.
    Module-level non-class statements are emitted with class_name=None.
    For ClassDef, only direct method members (FunctionDef/AsyncFunctionDef) are emitted
    — the ClassDef itself is intentionally skipped to avoid a full-body hash node that
    would cause false-positive drift whenever any sibling method changes.
    Non-method class body members (variables, docstrings) are benign and get no node.
    """
    result: list[tuple[ast.stmt, str | None]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            for member in stmt.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result.append((member, stmt.name))
        else:
            result.append((stmt, None))
    return result


def _hash(source: str, stmt: ast.stmt) -> str:
    segment = ast.get_source_segment(source, stmt) or ""
    return hashlib.sha256(segment.encode()).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_absolute(module: str, project_root: Path) -> str | None:
    parts = module.replace(".", "/")
    for suffix in (f"{parts}.py", f"{parts}/__init__.py"):
        candidate = project_root / suffix
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def resolve_import(
    stmt: ast.Import | ast.ImportFrom,
    from_file: str,
    project_root: str,
) -> list[str]:
    """
    Resolve an import statement to absolute file paths within the project.
    Returns empty list for stdlib/third-party imports.
    Dynamic imports (importlib.import_module with variable target) are unresolvable and silently skipped.
    """
    root = Path(project_root).resolve()
    origin = Path(from_file).resolve()
    results: list[str] = []

    if isinstance(stmt, ast.ImportFrom):
        level = stmt.level or 0
        module = stmt.module or ""

        if level > 0:
            base = origin.parent
            for _ in range(level - 1):
                base = base.parent
            candidate = (base / module.replace(".", "/")) if module else base
            for p in (candidate.with_suffix(".py"), candidate / "__init__.py"):
                if p.is_file() and _is_within(p, root):
                    results.append(str(p.resolve()))
                    break
        else:
            resolved = _resolve_absolute(module, root)
            if resolved:
                results.append(resolved)

    elif isinstance(stmt, ast.Import):
        for alias in stmt.names:
            resolved = _resolve_absolute(alias.name, root)
            if resolved:
                results.append(resolved)

    return [r for r in results if r != str(origin)]


def _parse(file_path: str) -> tuple[str, ast.Module]:
    source = Path(file_path).read_text(encoding="utf-8")
    return source, ast.parse(source, filename=file_path)


def is_fresh(file_path: str, conn: sqlite3.Connection) -> bool:
    """
    Return True if every stored node hash matches current on-disk content.
    Used as a guard to skip redundant re-indexing when PostToolUse already
    updated the cache before a FileChanged event fires for the same edit.
    """
    abs_path = str(Path(file_path).resolve())
    stored = {r[0]: r[1] for r in conn.execute(
        "SELECT node_id, content_hash FROM nodes WHERE file_path = ?", (abs_path,)
    ).fetchall()}
    if not stored:
        return False
    try:
        source, tree = _parse(abs_path)
    except Exception:
        return False
    current = {
        _node_id(abs_path, stmt, cls): _hash(source, stmt)
        for stmt, cls in _all_stmts(tree)
    }
    return stored == current


def parse_file(file_path: str) -> list[Node]:
    """Extract all indexable nodes from a Python file, including class methods."""
    abs_path = str(Path(file_path).resolve())
    source, tree = _parse(abs_path)
    return [
        Node(
            node_id=_node_id(abs_path, stmt, cls),
            file_path=abs_path,
            name=_name(stmt, cls),
            kind=_classify(stmt),
            line_start=stmt.lineno,
            line_end=stmt.end_lineno or stmt.lineno,
            content_hash=_hash(source, stmt),
        )
        for stmt, cls in _all_stmts(tree)
    ]


def index_file(file_path: str, project_root: str, conn: sqlite3.Connection) -> None:
    """
    Parse a file and upsert its nodes and outbound edges into the DB.
    Existing rows for this file are replaced atomically — handles additions,
    modifications, and removals in one pass.
    """
    abs_path = str(Path(file_path).resolve())
    source, tree = _parse(abs_path)

    nodes = [
        Node(
            node_id=_node_id(abs_path, stmt, cls),
            file_path=abs_path,
            name=_name(stmt, cls),
            kind=_classify(stmt),
            line_start=stmt.lineno,
            line_end=stmt.end_lineno or stmt.lineno,
            content_hash=_hash(source, stmt),
        )
        for stmt, cls in _all_stmts(tree)
    ]

    import_stmts = [s for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom))]
    edges = []
    for stmt in import_stmts:
        for target in resolve_import(stmt, abs_path, project_root):
            edges.append((abs_path, target))

    with conn:
        conn.execute("DELETE FROM nodes WHERE file_path = ?", (abs_path,))
        conn.execute("DELETE FROM edges WHERE from_file = ?", (abs_path,))

        conn.executemany(
            "INSERT INTO nodes (node_id, file_path, name, kind, line_start, line_end, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(n.node_id, n.file_path, n.name, n.kind, n.line_start, n.line_end, n.content_hash) for n in nodes],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO edges (from_file, to_file) VALUES (?, ?)",
            edges,
        )


def crawl_subgraph(
    start_file: str,
    depth: int,
    conn: sqlite3.Connection,
    direction: Literal["forward", "reverse", "both"] = "forward",
) -> set[str]:
    """
    BFS from start_file up to depth hops over the import edge graph.
    forward  — files this file depends on
    reverse  — files that depend on this file
    both     — full neighbourhood
    Returns the set of absolute file paths including start_file.
    """
    start = str(Path(start_file).resolve())
    visited: set[str] = {start}
    frontier: set[str] = {start}

    for _ in range(depth):
        next_frontier: set[str] = set()
        for f in frontier:
            if direction in ("forward", "both"):
                rows = conn.execute("SELECT to_file FROM edges WHERE from_file = ?", (f,)).fetchall()
                next_frontier.update(r[0] for r in rows)
            if direction in ("reverse", "both"):
                rows = conn.execute("SELECT from_file FROM edges WHERE to_file = ?", (f,)).fetchall()
                next_frontier.update(r[0] for r in rows)
        next_frontier -= visited
        if not next_frontier:
            break
        visited |= next_frontier
        frontier = next_frontier

    return visited


def compute_merkle_root(file_paths: set[str], conn: sqlite3.Connection) -> str:
    """
    Compute a Merkle root over a subgraph.
    Leaf hash: SHA256 of a file's node content_hashes concatenated in line order.
    Root: SHA256 of sorted leaf hashes.
    Used as a cheap first-pass drift check — root match implies all nodes match.
    """
    leaf_hashes: list[str] = []
    for fp in sorted(file_paths):
        rows = conn.execute(
            "SELECT content_hash FROM nodes WHERE file_path = ? ORDER BY line_start",
            (fp,),
        ).fetchall()
        if rows:
            leaf = hashlib.sha256(b"".join(r[0].encode() for r in rows)).hexdigest()
            leaf_hashes.append(leaf)

    return hashlib.sha256(b"".join(h.encode() for h in leaf_hashes)).hexdigest()


def update_file(file_path: str, project_root: str, conn: sqlite3.Connection) -> None:
    """
    Re-index a file after a write completes (PostToolUse / FileChanged).
    Graph extensions (new imports added) and deletions are both handled by
    index_file's delete-then-reinsert approach.

    Structural deletions: if nodes disappear (or the file is removed entirely),
    all files with inbound edges to this file are re-indexed so their edge lists
    reflect current state and orphaned edges are purged.
    """
    abs_path = str(Path(file_path).resolve())

    prior_node_ids = {r[0] for r in conn.execute(
        "SELECT node_id FROM nodes WHERE file_path = ?", (abs_path,)
    ).fetchall()}

    if not Path(abs_path).is_file():
        # File deleted — query dependents before purging edges, then clean up.
        dependent_files = [r[0] for r in conn.execute(
            "SELECT from_file FROM edges WHERE to_file = ?", (abs_path,)
        ).fetchall()]
        with conn:
            conn.execute("DELETE FROM nodes WHERE file_path = ?", (abs_path,))
            conn.execute("DELETE FROM edges WHERE from_file = ? OR to_file = ?", (abs_path, abs_path))
        for dep in dependent_files:
            if Path(dep).is_file():
                try:
                    index_file(dep, project_root, conn)
                except Exception:
                    pass
        return

    index_file(file_path, project_root, conn)

    if prior_node_ids:
        new_node_ids = {r[0] for r in conn.execute(
            "SELECT node_id FROM nodes WHERE file_path = ?", (abs_path,)
        ).fetchall()}
        if prior_node_ids - new_node_ids:
            dependent_files = [r[0] for r in conn.execute(
                "SELECT from_file FROM edges WHERE to_file = ?", (abs_path,)
            ).fetchall()]
            for dep in dependent_files:
                if dep != abs_path and Path(dep).is_file():
                    try:
                        index_file(dep, project_root, conn)
                    except Exception:
                        pass


def identify_target_nodes(
    file_path: str,
    tool_name: str,
    tool_input: dict,
    conn: sqlite3.Connection,
) -> list[str]:
    """
    Determine which node_ids in file_path are targeted by this tool call.

    Edit / MultiEdit: match old_string position to node line ranges.
    Write: all nodes in the file (full replacement — caller filters to structural).
    Returns empty list when old_string falls outside any named node (whitespace, comments).
    """
    abs_path = str(Path(file_path).resolve())

    if tool_name == "Write":
        return [r[0] for r in conn.execute(
            "SELECT node_id FROM nodes WHERE file_path = ?", (abs_path,)
        ).fetchall()]

    old_strings: list[str] = []
    if tool_name == "Edit":
        s = tool_input.get("old_string", "")
        if s:
            old_strings.append(s)
    elif tool_name == "MultiEdit":
        for edit in tool_input.get("edits", []):
            s = edit.get("old_string", "")
            if s:
                old_strings.append(s)

    if not old_strings:
        return []

    try:
        source = Path(abs_path).read_text(encoding="utf-8")
    except OSError:
        return []

    rows = conn.execute(
        "SELECT node_id, line_start, line_end FROM nodes WHERE file_path = ? ORDER BY line_start",
        (abs_path,),
    ).fetchall()
    if not rows:
        return []

    matched: set[str] = set()
    for old_string in old_strings:
        # Find ALL occurrences to handle non-unique old_strings correctly; for MultiEdit,
        # later edits may not exist in current source yet (created by an earlier edit in
        # the same batch) — those are skipped and covered by the first edit's node locks.
        search_start = 0
        while True:
            offset = source.find(old_string, search_start)
            if offset == -1:
                break
            edit_line_start = source[:offset].count("\n") + 1
            edit_line_end   = edit_line_start + old_string.count("\n")
            for node_id, line_start, line_end in rows:
                if line_start <= edit_line_end and line_end >= edit_line_start:
                    matched.add(node_id)
            search_start = offset + 1

    return list(matched)


def compute_merkle_root_from_node_ids(
    node_ids: list[str],
    conn: sqlite3.Connection,
) -> str:
    """
    Recompute a Merkle root directly from stored node_ids without re-crawling the filesystem.
    Used at PostToolUse: fetches current content_hashes from the nodes table, which still
    reflect pre-edit state until update_file() runs. Drift shows up as a root mismatch.
    """
    if not node_ids:
        return hashlib.sha256(b"").hexdigest()

    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"SELECT file_path, content_hash FROM nodes WHERE node_id IN ({placeholders})"
        " ORDER BY file_path, line_start",
        node_ids,
    ).fetchall()

    by_file: dict[str, list[str]] = {}
    for file_path, content_hash in rows:
        by_file.setdefault(file_path, []).append(content_hash)

    leaf_hashes = [
        hashlib.sha256(b"".join(h.encode() for h in hashes)).hexdigest()
        for _, hashes in sorted(by_file.items())
    ]
    return hashlib.sha256(b"".join(h.encode() for h in leaf_hashes)).hexdigest()
