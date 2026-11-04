"""
impact.py — the dependency graph as a library for CI consumers, without the
hook coordinator: which nodes a change touched, which files depend on them,
what effects they have, and a cache key for a file's whole dependency closure.

Typical use, test-impact selection on a pull request:

    graph = impact.index_project(repo_root)
    changes = impact.changed_files(repo_root, base_sha, head_sha)
    affected = impact.reverse_closure(graph, [c.path for c in changes])
    tests = [f for f in affected if is_test(f)]

Selection policy (which changes force a full run, which tests always run)
belongs to the consumer: this module only answers graph questions, and it
answers them conservatively. Paths are absolute in and out unless a
function says otherwise; closure hashes use project-relative paths so they
match across checkouts.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lomitus import dep_graph as dg

ChangeKind = Literal["added", "removed", "modified", "unparseable"]

# Directories never indexed by index_project.
DEFAULT_EXCLUDE: frozenset[str] = frozenset({
    ".git", ".hg", ".venv", "venv", ".tox", ".nox", "node_modules", "__pycache__",
    "build", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".claude",
})


@dataclass(frozen=True)
class FileChange:
    path: str                 # project-relative, POSIX separators (the new path for renames)
    status: Literal["added", "deleted", "modified", "renamed"]
    old_path: str | None = None


@dataclass(frozen=True)
class NodeChange:
    path: str                 # project-relative
    label: str                # dep_graph's stable node label, e.g. "Cls.meth", "var:LIMIT"
    name: str | None          # function/method/class qualname, None for other statements
    change: ChangeKind
    is_function: bool


def _python_files(root: Path, exclude: frozenset[str]) -> list[str]:
    out = []
    for p in root.rglob("*.py"):
        rel_parts = p.relative_to(root).parts
        if exclude.intersection(rel_parts[:-1]):
            continue
        out.append(str(p.resolve()))
    return sorted(out)


def index_project(
    project_root: str | Path,
    conn: sqlite3.Connection | None = None,
    exclude: Iterable[str] = DEFAULT_EXCLUDE,
    effect_passes: int = 3,
) -> sqlite3.Connection:
    """
    Index every Python file under project_root into conn (a fresh in-memory
    DB by default) and return it. Effect typing crosses one file boundary per
    pass, so after the full first pass it re-indexes only the importers of
    files whose kinds could have changed, until nothing changes, for at most
    `effect_passes` passes in all. Files that don't parse are skipped: they have no
    nodes and no outgoing edges (a consumer should treat a change to one as
    "run everything").
    """
    root = Path(project_root).resolve()
    if conn is None:
        conn = sqlite3.connect(":memory:")
    conn.executescript(dg.SCHEMA)
    files = _python_files(root, frozenset(exclude))
    file_set = set(files)

    def index(batch: Iterable[str]) -> None:
        for f in batch:
            try:
                dg.index_file(f, str(root), conn)
            except (SyntaxError, UnicodeDecodeError, ValueError):
                with conn:
                    conn.execute("DELETE FROM nodes WHERE file_path = ?", (f,))
                    conn.execute("DELETE FROM edges WHERE from_file = ?", (f,))

    def kinds_by_file() -> dict[str, frozenset[tuple[str, str]]]:
        out: dict[str, set[tuple[str, str]]] = {}
        for f, nid, k in conn.execute("SELECT file_path, node_id, kind FROM nodes"):
            out.setdefault(f, set()).add((nid, k))
        return {f: frozenset(v) for f, v in out.items()}

    dg._resolve_memo.clear()
    dg._parse_memo.clear()
    dg._memo_held = True
    try:
        index(files)
        # A file indexed before a file it imports saw no kinds there yet, and
        # only "external" lookups matter. So the second pass re-indexes the
        # importers of files with an external node; later passes, the
        # importers of files whose kinds changed in the pass before.
        sources = {r[0] for r in conn.execute("SELECT DISTINCT file_path FROM nodes WHERE kind = 'external'")}
        for _ in range(max(0, effect_passes - 1)):
            if not sources:
                break
            dirty = dg._fetch_neighbors_batched(
                conn, "SELECT from_file FROM edges WHERE to_file IN ({})", sorted(sources)
            ) & file_set
            if not dirty:
                break
            before = kinds_by_file()
            index(sorted(dirty))
            after = kinds_by_file()
            sources = {f for f in dirty if before.get(f) != after.get(f)}
    finally:
        dg._memo_held = False
        dg._resolve_memo.clear()
        dg._parse_memo.clear()
    return conn


def _repo(repo_path: str | Path):
    import pygit2  # a declared dependency; imported here so the graph code doesn't need it

    return pygit2, pygit2.Repository(str(repo_path))


def _commit_tree(pygit2, repo, rev: str):
    obj = repo.revparse_single(rev)
    commit = obj.peel(pygit2.Commit)
    return commit.tree


def changed_files(repo_path: str | Path, base: str, head: str, find_renames: bool = True) -> list[FileChange]:
    """Files that differ between two revisions (any revspec), relative to
    the repository root. Renames are detected when find_renames is set."""
    pygit2, repo = _repo(repo_path)
    diff = repo.diff(_commit_tree(pygit2, repo, base), _commit_tree(pygit2, repo, head))
    if find_renames:
        diff.find_similar()
    out: list[FileChange] = []
    for d in diff.deltas:
        code = d.status_char()
        if code == "D":
            out.append(FileChange(d.old_file.path, "deleted"))
        elif code == "R":
            out.append(FileChange(d.new_file.path, "renamed", d.old_file.path))
        elif code == "A":
            out.append(FileChange(d.new_file.path, "added"))
        else:
            out.append(FileChange(d.new_file.path, "modified"))
    return out


def _blob_text(repo, tree, path: str) -> str | None:
    try:
        entry = tree[path]
    except KeyError:
        return None
    data = repo[entry.id].data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _node_hashes(source: str, path: str, hash_mode: dg.HashMode) -> dict[str, tuple[str, str | None, bool]] | None:
    """label -> (hash, name, is_function), or None when the source doesn't parse."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return None
    return {
        label: (dg._hash(source, stmt, hash_mode), dg._name(stmt, cls), isinstance(stmt, dg._DEFS))
        for stmt, cls, label in dg._labelled_stmts(tree)
    }


def changed_nodes(
    repo_path: str | Path,
    base: str,
    head: str,
    hash_mode: dg.HashMode = "ast",
    paths: Iterable[str] | None = None,
) -> list[NodeChange]:
    """
    Nodes that differ between two revisions, per Python file, read from the
    git objects (no checkout needed). With hash_mode="ast" (the default)
    formatting- and comment-only edits produce no change; "source" counts
    any textual change inside a node. A file that doesn't parse on either
    side yields one NodeChange(label="<file>", change="unparseable"). A
    renamed file is compared old path against new path.
    """
    pygit2, repo = _repo(repo_path)
    old_tree = _commit_tree(pygit2, repo, base)
    new_tree = _commit_tree(pygit2, repo, head)
    wanted = set(paths) if paths is not None else None
    out: list[NodeChange] = []
    for fc in changed_files(repo_path, base, head):
        if not fc.path.endswith(".py") and not (fc.old_path or "").endswith(".py"):
            continue
        if wanted is not None and fc.path not in wanted:
            continue
        old_src = _blob_text(repo, old_tree, fc.old_path or fc.path) if fc.status != "added" else ""
        new_src = _blob_text(repo, new_tree, fc.path) if fc.status != "deleted" else ""
        old = _node_hashes(old_src or "", fc.old_path or fc.path, hash_mode) if old_src is not None else None
        new = _node_hashes(new_src or "", fc.path, hash_mode) if new_src is not None else None
        if old is None or new is None:
            out.append(NodeChange(fc.path, "<file>", None, "unparseable", False))
            continue
        for label in sorted(set(old) | set(new)):
            o, n = old.get(label), new.get(label)
            if o is not None and n is not None and o[0] == n[0]:
                continue
            ref = n or o
            assert ref is not None
            kind: ChangeKind = "added" if o is None else "removed" if n is None else "modified"
            out.append(NodeChange(fc.path, label, ref[1], kind, ref[2]))
    return out


def _walk(conn: sqlite3.Connection, start: Iterable[str], query: str) -> set[str]:
    seen = {str(Path(s).resolve()) for s in start}
    frontier = list(seen)
    while frontier:
        nxt = dg._fetch_neighbors_batched(conn, query, frontier) - seen
        seen |= nxt
        frontier = list(nxt)
    return seen


def reverse_closure(conn: sqlite3.Connection, files: Iterable[str]) -> set[str]:
    """`files` plus every file that imports one of them, directly or
    transitively (unbounded depth). Paths are absolute."""
    return _walk(conn, files, "SELECT from_file FROM edges WHERE to_file IN ({})")


def forward_closure(conn: sqlite3.Connection, files: Iterable[str]) -> set[str]:
    """`files` plus every project file they import, transitively."""
    return _walk(conn, files, "SELECT to_file FROM edges WHERE from_file IN ({})")


def node_effects(conn: sqlite3.Connection, file: str | None = None) -> dict[tuple[str, str], str]:
    """{(absolute file, qualname): "external" | "structural" | "benign"} for
    every named node (functions, methods, class headers), optionally for one
    file. "external" means the node reaches a known I/O, network or database
    module, directly or through resolved calls (see dep_graph._classify_module)."""
    if file is None:
        rows = conn.execute("SELECT file_path, name, kind FROM nodes WHERE name IS NOT NULL")
    else:
        rows = conn.execute(
            "SELECT file_path, name, kind FROM nodes WHERE name IS NOT NULL AND file_path = ?",
            (str(Path(file).resolve()),),
        )
    return {(f, n): k for f, n, k in rows}


def file_digest(path: str | Path, hash_mode: dg.HashMode = "ast") -> str:
    """SHA256 over a file's (label, node hash) pairs. With "ast", formatting
    and comments don't change it. An unparseable file digests its raw bytes."""
    data = Path(path).read_bytes()
    try:
        source = data.decode("utf-8")
        hashes = _node_hashes(source, str(path), hash_mode)
    except UnicodeDecodeError:
        hashes = None
    if hashes is None:
        return hashlib.sha256(b"raw\0" + data).hexdigest()
    material = "\n".join(f"{label}\0{h}" for label, (h, _n, _f) in sorted(hashes.items()))
    return hashlib.sha256(material.encode()).hexdigest()


def closure_hash(
    conn: sqlite3.Connection,
    file: str | Path,
    project_root: str | Path,
    hash_mode: dg.HashMode = "ast",
    extra: Iterable[str] = (),
) -> str:
    """
    A Merkle root over `file` and its whole forward import closure: SHA256
    of the sorted (project-relative path, file_digest) leaves. Stable across
    checkouts and machines. Equal hashes mean no Python code the file can
    import changed (semantically, with "ast"), so a consumer may reuse a
    cached test result for it. `extra` mixes in anything else the result
    depends on (lockfile digest, Python version, environment), and non-Python
    inputs a test reads are NOT covered: include their digests via `extra`.
    """
    root = Path(project_root).resolve()
    leaves = []
    for f in sorted(forward_closure(conn, [str(file)])):
        p = Path(f)
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = p.as_posix()
        leaves.append(f"{rel}\0{file_digest(p, hash_mode)}")
    leaves.extend(f"extra\0{e}" for e in extra)
    return hashlib.sha256("\n".join(leaves).encode()).hexdigest()


def to_relative(paths: Iterable[str], project_root: str | Path) -> set[str]:
    """Absolute paths -> project-relative POSIX paths (others dropped)."""
    root = Path(project_root).resolve()
    out = set()
    for p in paths:
        try:
            out.add(Path(p).resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return out


def to_absolute(paths: Iterable[str], project_root: str | Path) -> list[str]:
    root = Path(project_root).resolve()
    return [str((root / p).resolve()) for p in paths]
