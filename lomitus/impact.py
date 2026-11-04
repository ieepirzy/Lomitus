"""
impact.py — the dependency graph as a library for CI consumers, without the
hook coordinator: which nodes a change touched, which files depend on them,
what effects they have, and a cache key for a file's whole dependency closure.

Typical use, test-impact selection on a pull request (run from the project
root, which may be a subdirectory of the repository):

    affected = impact.affected_files(".", base_sha, head_sha)
    tests = [f for f in affected if is_test(f)]

affected_files resolves importers of changed and added files on the head
graph, and importers of deleted files and of renamed files' old paths on
the base revision's graph, so a test that still imports a module the change
deleted or moved is selected.

Selection policy (which changes force a full run, which tests always run)
belongs to the consumer: this module only answers graph questions, and it
answers them conservatively. Path conventions are stated per function:
changed_files / changed_nodes / affected_files / importers_at speak
project-relative POSIX paths; the graph functions (reverse_closure,
forward_closure, node_effects) speak absolute paths; to_relative and
to_absolute convert.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import sys
import tempfile
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lomitus import dep_graph as dg

ChangeKind = Literal["added", "removed", "modified", "unparseable", "reordered", "deleted", "renamed"]

# Directory names never indexed by index_project, at any depth.
DEFAULT_EXCLUDE: frozenset[str] = frozenset({
    ".git", ".hg", ".venv", "venv", ".tox", ".nox", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".claude",
})
# Excluded only directly under the project root: build outputs. A package
# directory named "build" deeper down (e.g. src/app/build/) is source.
TOP_LEVEL_EXCLUDE: frozenset[str] = frozenset({"build", "dist"})

META_SCHEMA = "CREATE TABLE IF NOT EXISTS lomitus_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"


@dataclass(frozen=True)
class FileChange:
    path: str                 # project-relative, POSIX (the new path for renames)
    status: Literal["added", "deleted", "modified", "renamed"]
    old_path: str | None = None   # project-relative old path of a rename (None if it came from outside)


@dataclass(frozen=True)
class NodeChange:
    path: str                 # project-relative
    label: str                # dep_graph's stable node label ("Cls.meth", "var:LIMIT"), or "<file>" / "<order>"
    name: str | None          # function/method/class qualname, None for other statements
    change: ChangeKind
    is_function: bool


def _python_files(root: Path, exclude: frozenset[str], top_level_exclude: frozenset[str] = TOP_LEVEL_EXCLUDE) -> list[str]:
    out = []
    for p in root.rglob("*.py"):
        rel_parts = p.relative_to(root).parts
        dirs = rel_parts[:-1]
        if exclude.intersection(dirs) or (dirs and dirs[0] in top_level_exclude):
            continue
        out.append(str(p.resolve()))
    return sorted(out)


def index_project(
    project_root: str | Path,
    conn: sqlite3.Connection | None = None,
    exclude: Iterable[str] = DEFAULT_EXCLUDE,
    effect_passes: int = 10,
) -> sqlite3.Connection:
    """
    Index every Python file under project_root into conn (a fresh in-memory
    DB by default) and return it. Effect typing crosses one file boundary per
    pass: after the full first pass, each later pass re-indexes only the
    importers (by edge, which includes resolved qualified-call targets) of
    files whose kinds could have changed, until a pass changes nothing: a
    fixpoint. `effect_passes` caps the passes; if the cap is hit first, a
    RuntimeWarning is issued and index_stats(conn)["effects_converged"] is
    "false" ("not computed" with effect_passes <= 1, for graph-only use).
    Files that don't parse are skipped: they have no nodes and no
    outgoing edges (a consumer should treat a change to one as "run
    everything").
    """
    root = Path(project_root).resolve()
    if conn is None:
        conn = sqlite3.connect(":memory:")
    conn.executescript(dg.SCHEMA)
    conn.executescript(META_SCHEMA)
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
    passes = 1
    converged: bool | None = True
    try:
        index(files)
        # A file indexed before a file it imports saw no kinds there yet, and
        # only "external" lookups matter. So the second pass re-indexes the
        # importers of files with an external node; later passes, the
        # importers of files whose kinds changed in the pass before.
        sources = {r[0] for r in conn.execute("SELECT DISTINCT file_path FROM nodes WHERE kind = 'external'")}
        if effect_passes <= 1:
            sources, converged = set(), None  # effects deliberately not settled (graph-only use)
        while sources:
            dirty = dg._fetch_neighbors_batched(
                conn, "SELECT from_file FROM edges WHERE to_file IN ({})", sorted(sources)
            ) & file_set
            if not dirty:
                break
            if passes >= max(1, effect_passes):
                converged = False
                break
            passes += 1
            before = kinds_by_file()
            index(sorted(dirty))
            after = kinds_by_file()
            sources = {f for f in dirty if before.get(f) != after.get(f)}
    finally:
        dg._memo_held = False
        dg._resolve_memo.clear()
        dg._parse_memo.clear()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO lomitus_meta (key, value) VALUES (?, ?)",
            [("effect_passes", str(passes)),
             ("effects_converged", "not computed" if converged is None else "true" if converged else "false")],
        )
    if converged is False:
        warnings.warn(
            f"lomitus: effect typing did not converge within {effect_passes} passes; "
            "some cross-file effects may be missing",
            RuntimeWarning,
            stacklevel=2,
        )
    return conn


def index_stats(conn: sqlite3.Connection) -> dict[str, str]:
    """What index_project recorded: effect_passes, effects_converged."""
    try:
        return dict(conn.execute("SELECT key, value FROM lomitus_meta").fetchall())
    except sqlite3.OperationalError:
        return {}


def _repo(repo_path: str | Path):
    import pygit2  # a declared dependency; imported here so the graph code doesn't need it

    return pygit2, pygit2.Repository(str(repo_path))


def _commit_tree(pygit2, repo, rev: str):
    obj = repo.revparse_single(rev)
    commit = obj.peel(pygit2.Commit)
    return commit.tree


def _project_prefix(repo, project_root: str | Path) -> str:
    """The project root as a repo-relative POSIX prefix ("" or "svc/")."""
    workdir = Path(repo.workdir).resolve()
    rel = Path(project_root).resolve().relative_to(workdir).as_posix()
    return "" if rel == "." else rel.rstrip("/") + "/"


def _strip(prefix: str, repo_path: str) -> str | None:
    if not prefix:
        return repo_path
    return repo_path[len(prefix):] if repo_path.startswith(prefix) else None


def changed_files(
    repo_path: str | Path,
    base: str,
    head: str,
    find_renames: bool = True,
    project_root: str | Path | None = None,
) -> list[FileChange]:
    """
    Files that differ between two revisions (any revspec), relative to the
    project root: `project_root`, else `repo_path` itself, which may be a
    subdirectory of the repository (the repository is found by searching
    upward). Changes outside the project root are left out; a rename into
    the project from outside reports old_path=None. Renames are detected
    when find_renames is set.
    """
    pygit2, repo = _repo(repo_path)
    prefix = _project_prefix(repo, project_root if project_root is not None else repo_path)
    diff = repo.diff(_commit_tree(pygit2, repo, base), _commit_tree(pygit2, repo, head))
    if find_renames:
        diff.find_similar()
    out: list[FileChange] = []
    for d in diff.deltas:
        code = d.status_char()
        old = _strip(prefix, d.old_file.path)
        new = _strip(prefix, d.new_file.path)
        if code == "D":
            if old is not None:
                out.append(FileChange(old, "deleted"))
        elif code == "R":
            if new is not None:
                out.append(FileChange(new, "renamed", old))
            elif old is not None:  # renamed out of the project: gone from here
                out.append(FileChange(old, "deleted"))
        elif code == "A":
            if new is not None:
                out.append(FileChange(new, "added"))
        elif new is not None:
            out.append(FileChange(new, "modified"))
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
    """label -> (hash, name, is_function) in source order, or None when the
    source doesn't parse."""
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
    project_root: str | Path | None = None,
) -> list[NodeChange]:
    """
    Nodes that differ between two revisions, per Python file, read from the
    git objects (no checkout needed). Paths are project-relative (see
    changed_files). With hash_mode="ast" (the default) formatting- and
    comment-only edits produce no change; "source" counts any textual change
    inside a node. File-level entries (label "<file>", is_function False):

      deleted      the file is gone (its nodes are reported "removed" too);
      renamed      the file moved, even with identical content: importers of
                   the old path are not on the head graph (affected_files
                   resolves them on the base graph);
      unparseable  either side doesn't parse.

    And label "<order>", change "reordered": the file's top-level statements
    kept their content but changed order (import-time order matters).
    """
    pygit2, repo = _repo(repo_path)
    root = project_root if project_root is not None else repo_path
    prefix = _project_prefix(repo, root)
    old_tree = _commit_tree(pygit2, repo, base)
    new_tree = _commit_tree(pygit2, repo, head)
    wanted = set(paths) if paths is not None else None
    out: list[NodeChange] = []
    for fc in changed_files(repo_path, base, head, project_root=root):
        if not fc.path.endswith(".py") and not (fc.old_path or "").endswith(".py"):
            continue
        if wanted is not None and fc.path not in wanted and fc.old_path not in wanted:
            continue
        if fc.status == "deleted":
            out.append(NodeChange(fc.path, "<file>", None, "deleted", False))
        elif fc.status == "renamed":
            out.append(NodeChange(fc.path, "<file>", None, "renamed", False))
        old_path = fc.old_path if fc.status == "renamed" else fc.path
        old_src = _blob_text(repo, old_tree, prefix + old_path) if fc.status != "added" and old_path else ""
        new_src = _blob_text(repo, new_tree, prefix + fc.path) if fc.status != "deleted" else ""
        old = _node_hashes(old_src or "", old_path or fc.path, hash_mode) if old_src is not None else None
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
        common_old = [lb for lb in old if lb in new]
        common_new = [lb for lb in new if lb in old]
        if common_old != common_new:
            out.append(NodeChange(fc.path, "<order>", None, "reordered", False))
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


def _materialize(repo, tree, prefix: str, dest: Path) -> None:
    """Write the project's Python files and pyproject.toml at a revision to
    dest (no checkout of the working tree)."""
    def walk(t, at: str) -> None:
        for entry in t:
            path = f"{at}{entry.name}"
            if entry.type_str == "tree":
                # Into the project, or down the path that leads to it.
                if (path + "/").startswith(prefix) or prefix.startswith(path + "/"):
                    walk(repo[entry.id], path + "/")
            elif path.startswith(prefix) and (path.endswith(".py") or path == f"{prefix}pyproject.toml"):
                target = dest / path[len(prefix):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(repo[entry.id].data)
    walk(tree, "")


def importers_at(
    repo_path: str | Path,
    rev: str,
    paths: Iterable[str],
    project_root: str | Path | None = None,
    exclude: Iterable[str] = DEFAULT_EXCLUDE,
) -> set[str]:
    """
    Project-relative files that imported any of `paths` (project-relative),
    directly or transitively, on the graph as it was at `rev`: built from
    the revision's git objects in a temporary directory. For deleted files
    and renamed files' old paths, whose importers the head graph can't see.
    """
    pygit2, repo = _repo(repo_path)
    prefix = _project_prefix(repo, project_root if project_root is not None else repo_path)
    tree = _commit_tree(pygit2, repo, rev)
    with tempfile.TemporaryDirectory(prefix="lomitus-rev-") as tmp:
        dest = Path(tmp).resolve()
        _materialize(repo, tree, prefix, dest)
        dg._roots_for.cache_clear()
        conn = index_project(dest, exclude=exclude, effect_passes=1)
        try:
            closure = reverse_closure(conn, to_absolute(paths, dest))
            return to_relative(closure, dest)
        finally:
            conn.close()
            dg._roots_for.cache_clear()


def affected_files(
    repo_path: str | Path,
    base: str,
    head: str,
    conn: sqlite3.Connection | None = None,
    project_root: str | Path | None = None,
) -> set[str]:
    """
    Project-relative files a change can affect: the changed, added and
    renamed-to files and their importers on the head graph (`conn`, or
    index_project of the working tree, which must be at `head`), plus the
    importers of deleted files and of renamed files' old paths on the base
    revision's graph. Deleted paths themselves are not returned.
    """
    root = Path(project_root if project_root is not None else repo_path).resolve()
    changes = changed_files(repo_path, base, head, project_root=root)
    if conn is None:
        conn = index_project(root)
    present = [c.path for c in changes if c.status != "deleted"]
    out = to_relative(reverse_closure(conn, to_absolute(present, root, must_exist=False)), root)
    gone = [c.path for c in changes if c.status == "deleted"] + [
        c.old_path for c in changes if c.status == "renamed" and c.old_path
    ]
    if gone:
        out |= importers_at(repo_path, base, gone, project_root=root)
        out -= set(gone)
        out |= {p for p in present}  # a renamed-to path equal to another's old path stays
    return out


def node_effects(conn: sqlite3.Connection, file: str | None = None) -> dict[tuple[str, str], str]:
    """{(absolute file, qualname): "external" | "structural" | "benign"} for
    every named node (functions, methods, class headers), optionally for one
    file. "external" means the node reaches a known I/O, network or database
    module, directly or through resolved calls (see dep_graph._classify_module).
    A name defined twice (a property getter and setter, overloads) is
    "external" if any definition is."""
    if file is None:
        rows = conn.execute("SELECT file_path, name, kind FROM nodes WHERE name IS NOT NULL")
    else:
        rows = conn.execute(
            "SELECT file_path, name, kind FROM nodes WHERE name IS NOT NULL AND file_path = ?",
            (str(Path(file).resolve()),),
        )
    out: dict[tuple[str, str], str] = {}
    for f, n, k in rows:
        if out.get((f, n)) != "external":
            out[(f, n)] = k
    return out


def file_digest(path: str | Path, hash_mode: dg.HashMode = "ast") -> str:
    """SHA256 over a file's (label, node hash) pairs in source order: moving
    a top-level statement changes it (import-time order matters), formatting
    and comments don't (with "ast"). An unparseable file digests its raw bytes."""
    data = Path(path).read_bytes()
    try:
        source = data.decode("utf-8")
        hashes = _node_hashes(source, str(path), hash_mode)
    except UnicodeDecodeError:
        hashes = None
    if hashes is None:
        return hashlib.sha256(b"raw\0" + data).hexdigest()
    material = "\n".join(f"{label}\0{h}" for label, (h, _n, _f) in hashes.items())
    return hashlib.sha256(material.encode()).hexdigest()


def _conftests(file: Path, root: Path) -> list[Path]:
    """conftest.py files pytest loads for `file`: in its directory and every
    ancestor up to the project root."""
    out = []
    d = file.parent
    while d.is_relative_to(root):
        c = d / "conftest.py"
        if c.is_file():
            out.append(c.resolve())
        if d == root:
            break
        d = d.parent
    return out


def _pytest_plugins(conftest: Path, root: Path) -> list[str]:
    """Project modules a conftest names in a literal `pytest_plugins`."""
    try:
        tree = ast.parse(conftest.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return []
    names: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in stmt.targets):
            v = stmt.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                names.append(v.value)
            elif isinstance(v, (ast.List, ast.Tuple)):
                names += [e.value for e in v.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    roots = dg.import_roots(str(root))
    out = []
    for n in names:
        f = dg._resolve_module(n, roots, root)
        if f:
            out.append(f)
    return out


def closure_hash(
    conn: sqlite3.Connection,
    file: str | Path,
    project_root: str | Path,
    hash_mode: dg.HashMode = "ast",
    extra: Iterable[str] = (),
    include_python: bool = True,
) -> str:
    """
    A Merkle root over `file`, the conftest.py files pytest loads for it
    (its directory and every ancestor up to the project root), the project
    modules those conftests name in a literal `pytest_plugins`, and the
    whole forward import closure of all of them: SHA256 of the sorted
    (project-relative path, file_digest) leaves, plus the Python version
    (ast.dump output differs between Python versions, so hashes are only
    comparable within one). Stable across checkouts and machines.

    Code only: equal hashes mean no Python code the test can import changed.
    Non-Python inputs (fixtures, SQL, templates, data files), dependency
    versions (the lockfile), plugins enabled outside the project (`-p` in
    pytest config, installed entry points) and the environment are NOT
    covered: pass their digests via `extra`.
    """
    root = Path(project_root).resolve()
    start = [str(Path(file).resolve())]
    for c in _conftests(Path(file).resolve(), root):
        start.append(str(c))
        start.extend(_pytest_plugins(c, root))
    leaves = []
    for f in sorted(forward_closure(conn, start)):
        p = Path(f)
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = p.as_posix()
        leaves.append(f"{rel}\0{file_digest(p, hash_mode)}")
    if include_python:
        leaves.append(f"python\0{sys.version_info.major}.{sys.version_info.minor}")
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


def to_absolute(paths: Iterable[str], project_root: str | Path, must_exist: bool = True) -> list[str]:
    """Project-relative paths -> absolute. With must_exist (the default), a
    path that doesn't exist under project_root raises ValueError: a path
    relative to the wrong root (e.g. repo-relative for a project in a
    subdirectory) would otherwise select nothing, silently."""
    root = Path(project_root).resolve()
    out = []
    missing = []
    for p in paths:
        a = (root / p).resolve()
        if must_exist and not a.exists():
            missing.append(p)
        out.append(str(a))
    if missing:
        raise ValueError(
            f"{len(missing)} path(s) don't exist under {root}, e.g. {missing[0]!r}: are they "
            "relative to the repository root instead of the project root? (must_exist=False to allow)"
        )
    return out
