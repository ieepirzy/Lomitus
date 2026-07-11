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

from lomitus.contract import has_external_calls, import_aliases

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

@dataclass(frozen=True)
class Node:
    node_id: str
    file_path: str
    name: str | None
    kind: Literal["structural", "external", "benign"]
    line_start: int
    line_end: int
    content_hash: str


def _call_targets(stmt: ast.stmt) -> tuple[set[str], set[str]]:
    """
    Names called from within a function/method body that might reference
    another function defined in the same file.
    Returns (bare_names, self_or_cls_attrs):
      bare_names        — "helper()" — may be a top-level function, or an
                           imported project symbol (see _project_import_targets).
      self_or_cls_attrs — "self.helper()" / "cls.helper()" — a method on
                           the same class.
    """
    bare: set[str] = set()
    attrs: set[str] = set()
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                bare.add(func.id)
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in ("self", "cls")
            ):
                attrs.add(func.attr)
    return bare, attrs


def _classify_module(
    tree: ast.Module,
    aliases: dict[str, str] | None = None,
    extra_external: frozenset[str] = frozenset(),
) -> dict[str, Literal["structural", "external"]]:
    """
    Classify every function/method in a module as "structural" or "external",
    answering (to the extent AST-level static analysis can) "does the call
    graph of this function reach a node with an observable side effect at
    execution time":

      1. Direct effect: the function's own body calls a known external
         module (has_external_calls, alias-aware).
      2. Same-file transitive effect: the function calls another
         function/method defined in this file (bare name, or self./cls.
         attribute) that is (transitively) external.
      3. Cross-file transitive effect: 'extra_external' seeds names that are
         already known to be external via a resolved same-file-shaped call
         edge (see dep_graph.index_file, which resolves "from project_module
         import name" edges against already-indexed nodes).

    Propagation is a fixed-point BFS over the reversed call graph, so cycles
    (mutual recursion) terminate safely. Qualified calls through a project-
    internal module alias ("import mymod; mymod.fn()") and dynamic dispatch
    are not resolved — those stay conservatively "structural" unless some
    other reachable call is directly external.
    """
    if aliases is None:
        aliases = import_aliases(tree)

    kind: dict[str, Literal["structural", "external"]] = {}
    call_bare: dict[str, set[str]] = {}
    call_attr: dict[str, set[str]] = {}
    class_of: dict[str, str | None] = {}
    top_level: set[str] = set()

    for stmt, cls in _all_stmts(tree):
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        qualname = _name(stmt, cls)
        if qualname is None:
            continue
        kind[qualname] = "external" if has_external_calls(stmt, aliases) else "structural"
        call_bare[qualname], call_attr[qualname] = _call_targets(stmt)
        class_of[qualname] = cls
        if cls is None:
            top_level.add(qualname)

    for name in extra_external:
        if name not in kind:
            kind[name] = "external"
            top_level.add(name)

    reverse: dict[str, set[str]] = {q: set() for q in kind}
    for qualname, bare in call_bare.items():
        for name in bare:
            if name in top_level:
                reverse[name].add(qualname)
        cls = class_of.get(qualname)
        if cls is not None:
            for name in call_attr[qualname]:
                target = f"{cls}.{name}"
                if target in kind:
                    reverse[target].add(qualname)

    frontier = {q for q, k in kind.items() if k == "external"}
    seen = set(frontier)
    while frontier:
        nxt: set[str] = set()
        for node_name in frontier:
            for caller in reverse.get(node_name, ()):
                if caller not in seen:
                    seen.add(caller)
                    kind[caller] = "external"
                    nxt.add(caller)
        frontier = nxt

    return kind


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


def _project_import_targets(
    tree: ast.Module,
    from_file: str,
    project_root: str,
) -> dict[str, tuple[str, str]]:
    """
    Map bare names bound by "from project_module import name [as alias]" to
    (resolved_absolute_file, name) when project_module resolves to a file
    within the project. Used by index_file to look up whether an imported
    symbol is already classified "external" in another file's indexed nodes,
    so effect typing propagates across file boundaries, not just within one.
    Plain "import x" bindings are qualified at call sites (x.func()) and are
    not resolved here — see _classify_module's docstring for that limitation.
    """
    targets: dict[str, tuple[str, str]] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.ImportFrom):
            continue
        resolved = resolve_import(stmt, from_file, project_root)
        if not resolved:
            continue
        target_file = resolved[0]
        for alias in stmt.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            targets[bound] = (target_file, alias.name)
    return targets


def _lookup_kind(conn: sqlite3.Connection, file_path: str, name: str) -> str | None:
    row = conn.execute(
        "SELECT kind FROM nodes WHERE file_path = ? AND name = ?", (file_path, name)
    ).fetchone()
    return row[0] if row else None


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


def _kind_of(
    kind_map: dict[str, Literal["structural", "external"]],
    stmt: ast.stmt,
    class_name: str | None,
) -> Literal["structural", "external", "benign"]:
    name = _name(stmt, class_name)
    if name is None:
        return "benign"
    return kind_map.get(name, "benign")


def parse_file(file_path: str) -> list[Node]:
    """
    Extract all indexable nodes from a Python file, including class methods.
    Effect typing here is same-file only (no conn/project_root to look up
    cross-file classifications) — see index_file for the cross-file pass.
    """
    abs_path = str(Path(file_path).resolve())
    source, tree = _parse(abs_path)
    kind_map = _classify_module(tree)
    return [
        Node(
            node_id=_node_id(abs_path, stmt, cls),
            file_path=abs_path,
            name=_name(stmt, cls),
            kind=_kind_of(kind_map, stmt, cls),
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

    Effect typing (see _classify_module) propagates transitively within this
    file, plus one cross-file hop: for each "from project_module import name"
    binding, if project_module is already indexed and that name's node is
    "external", this file's callers of that name are seeded external too.
    Kept in sync on later changes via update_file's dependent re-indexing.
    """
    abs_path = str(Path(file_path).resolve())
    source, tree = _parse(abs_path)

    aliases = import_aliases(tree)
    import_targets = _project_import_targets(tree, abs_path, project_root)
    extra_external = frozenset(
        name
        for name, (target_file, target_name) in import_targets.items()
        if _lookup_kind(conn, target_file, target_name) == "external"
    )
    kind_map = _classify_module(tree, aliases=aliases, extra_external=extra_external)

    nodes = [
        Node(
            node_id=_node_id(abs_path, stmt, cls),
            file_path=abs_path,
            name=_name(stmt, cls),
            kind=_kind_of(kind_map, stmt, cls),
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

    Dependent re-indexing: all files with inbound edges to this file are
    re-indexed when either (a) this file's node set changed (structural
    deletions/additions — edge lists need to reflect current state and
    orphaned edges are purged) or (b) any node's effect-typing "kind"
    changed (e.g. a function gained/lost a network call) — dependents may
    import from this file and their own cross-file classification
    (index_file's extra_external lookup) needs to pick up the new value.
    This re-indexes only direct dependents (one hop); a change that flips a
    node's kind cascades further only as each affected file is itself
    subsequently updated.
    """
    abs_path = str(Path(file_path).resolve())

    prior_rows = {r[0]: r[1] for r in conn.execute(
        "SELECT node_id, kind FROM nodes WHERE file_path = ?", (abs_path,)
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

    if prior_rows:
        new_rows = {r[0]: r[1] for r in conn.execute(
            "SELECT node_id, kind FROM nodes WHERE file_path = ?", (abs_path,)
        ).fetchall()}
        node_ids_changed = set(prior_rows) != set(new_rows)
        kind_changed = any(new_rows.get(nid) != k for nid, k in prior_rows.items())
        if node_ids_changed or kind_changed:
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
