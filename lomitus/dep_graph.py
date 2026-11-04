#!/usr/bin/env python3
"""
dep_graph.py — module-level dependency graph construction for the CBS coordinator.

Nodes are module-level AST constructs (functions, methods, a header node per
class, imports, assignments, other statements). Edges are cross-file import
dependencies within the project, from imports at any nesting depth.
Hashes are SHA256 of node source content, not mtimes.

Node ids are stable under code motion: functions and methods by qualified
name, classes by name, imports and assignments by what they bind, any other
statement by a digest of its AST. A label seen twice in one file gets an
ordinal suffix ("x#2"), e.g. a property getter and its setter.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import os
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lomitus.contract import EXTERNAL_CALL_PREFIXES, _call_root, import_aliases

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

HashMode = Literal["source", "ast"]


@dataclass(frozen=True)
class Node:
    node_id: str
    file_path: str
    name: str | None
    kind: Literal["structural", "external", "benign"]
    line_start: int
    line_end: int
    content_hash: str


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _dotted(func: ast.expr) -> str | None:
    """'a.b.c' for an attribute chain rooted at a plain name, else None."""
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name) or not parts:
        return None
    parts.append(func.id)
    return ".".join(reversed(parts))


@dataclass(frozen=True)
class _Calls:
    bare: frozenset[str]      # "helper()"
    attrs: frozenset[str]     # "self.helper()" / "cls.helper()"
    dotted: frozenset[str]    # "mod.fn()", "pkg.mod.Cls.meth()"
    roots: frozenset[str]     # the root name of every call, for external detection


_calls_cache: dict[int, tuple[ast.AST, _Calls]] = {}


def _calls(stmt: ast.stmt) -> _Calls:
    """Every call shape in one walk of the function (memoized per AST node
    object, so the classifier and index_file never walk a function twice)."""
    hit = _calls_cache.get(id(stmt))
    if hit is not None and hit[0] is stmt:
        return hit[1]
    bare: set[str] = set()
    attrs: set[str] = set()
    dotted: set[str] = set()
    roots: set[str] = set()
    for node in ast.walk(stmt):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        root = _call_root(func)
        if root:
            roots.add(root)
        if isinstance(func, ast.Name):
            bare.add(func.id)
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
                attrs.add(func.attr)
            else:
                d = _dotted(func)
                if d and d.split(".", 1)[0] not in ("self", "cls"):
                    dotted.add(d)
    out = _Calls(frozenset(bare), frozenset(attrs), frozenset(dotted), frozenset(roots))
    if len(_calls_cache) > 50_000:
        _calls_cache.clear()
    _calls_cache[id(stmt)] = (stmt, out)
    return out


def _is_direct_external(stmt: ast.stmt, aliases: dict[str, str]) -> bool:
    """contract.has_external_calls, on the memoized call roots."""
    return any(aliases.get(r, r) in EXTERNAL_CALL_PREFIXES for r in _calls(stmt).roots)


def _call_targets(stmt: ast.stmt) -> tuple[set[str], set[str]]:
    """
    Names called from within a function/method body that might reference
    another function defined in the same file.
    Returns (bare_names, self_or_cls_attrs):
      bare_names        — "helper()" — may be a top-level function, or an
                           imported project symbol (see _project_import_targets).
      self_or_cls_attrs — "self.helper()" / "cls.helper()" — a method on
                           the same class.
    Qualified calls ("mod.fn()") are returned by _qualified_calls.
    """
    c = _calls(stmt)
    return set(c.bare), set(c.attrs)


def _qualified_calls(stmt: ast.stmt) -> set[str]:
    """Dotted call targets rooted at a name other than self/cls: "mod.fn",
    "pkg.mod.Cls.meth". Resolved against the file's import bindings by
    _resolve_qualified."""
    return set(_calls(stmt).dotted)


def _classify_module(
    tree: ast.Module,
    aliases: dict[str, str] | None = None,
    extra_external: frozenset[str] = frozenset(),
    external_qualified: frozenset[str] = frozenset(),
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
      3. Cross-file transitive effect: 'extra_external' seeds bare names that
         are already known to be external via a resolved "from project_module
         import name" binding (at any nesting depth), and
         'external_qualified' seeds dotted calls ("mod.fn()",
         "pkg.mod.Cls.meth()") that resolve through an import binding to a
         project node already classified external (see index_file).

    Propagation is a fixed-point BFS over the reversed call graph, so cycles
    (mutual recursion) terminate safely. Dynamic dispatch stays
    conservatively "structural" unless some other reachable call is directly
    external.
    """
    if aliases is None:
        aliases = import_aliases(tree)

    kind: dict[str, Literal["structural", "external"]] = {}
    call_bare: dict[str, set[str]] = {}
    call_attr: dict[str, set[str]] = {}
    class_of: dict[str, str | None] = {}
    top_level: set[str] = set()

    for stmt, cls in _all_stmts(tree):
        if not isinstance(stmt, _DEFS):
            continue
        qualname = _name(stmt, cls)
        if qualname is None:
            continue
        direct = _is_direct_external(stmt, aliases) or bool(
            external_qualified and (_calls(stmt).dotted & external_qualified)
        )
        if direct or kind.get(qualname) != "external":
            kind[qualname] = "external" if direct else kind.get(qualname, "structural")
        bare, attrs = _call_targets(stmt)
        call_bare.setdefault(qualname, set()).update(bare)
        call_attr.setdefault(qualname, set()).update(attrs)
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
    if isinstance(stmt, _DEFS):
        return f"{class_name}.{stmt.name}" if class_name else stmt.name
    if isinstance(stmt, ast.ClassDef):
        return stmt.name
    return None


def _ast_digest(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()[:12]


def _store_names(targets: list[ast.expr]) -> list[str]:
    names: list[str] = []
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.append(n.id)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
                d = _dotted(n)
                if d:
                    names.append(d)
    return names


def _label(stmt: ast.stmt, class_name: str | None) -> str:
    """The un-deduplicated label of a node: stable when the code moves."""
    named = _name(stmt, class_name)
    if named is not None:
        return named
    if isinstance(stmt, ast.Import):
        return "import:" + ",".join(a.asname or a.name for a in stmt.names)
    if isinstance(stmt, ast.ImportFrom):
        mod = "." * (stmt.level or 0) + (stmt.module or "")
        return f"from:{mod}:" + ",".join(a.asname or a.name for a in stmt.names)
    if isinstance(stmt, ast.Assign):
        names = _store_names(stmt.targets)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        names = _store_names([stmt.target])
    elif hasattr(ast, "TypeAlias") and isinstance(stmt, ast.TypeAlias):  # 3.12+
        names = _store_names([stmt.name])
    else:
        names = []
    if names:
        return "var:" + ",".join(names)
    return "stmt:" + _ast_digest(stmt)


def _all_stmts(tree: ast.Module) -> list[tuple[ast.stmt, str | None]]:
    """
    Return (stmt, class_name) for every indexable statement in a module.
    Module-level non-class statements are emitted with class_name=None.
    A ClassDef is emitted once as its *header* node (class_name=None, name =
    the class): decorators, bases, keywords and the non-method body members
    (fields, docstring). Its methods are emitted as their own nodes, and the
    header's hash leaves them out, so a method edit is never drift of the
    class node or of a sibling method.
    """
    result: list[tuple[ast.stmt, str | None]] = []
    for stmt in tree.body:
        result.append((stmt, None))
        if isinstance(stmt, ast.ClassDef):
            for member in stmt.body:
                if isinstance(member, _DEFS):
                    result.append((member, stmt.name))
    return result


def _labelled_stmts(tree: ast.Module) -> list[tuple[ast.stmt, str | None, str]]:
    """(stmt, class_name, label) with duplicate labels made unique by order
    of appearance: the first keeps its label, later ones get "#2", "#3"."""
    seen: dict[str, int] = {}
    out: list[tuple[ast.stmt, str | None, str]] = []
    for stmt, cls in _all_stmts(tree):
        base = _label(stmt, cls)
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append((stmt, cls, base if n == 1 else f"{base}#{n}"))
    return out


def _node_id(file_path: str, label: str) -> str:
    return f"{file_path}::{label}"


def _first_line(stmt: ast.stmt) -> int:
    decorators = getattr(stmt, "decorator_list", None) or []
    return min([stmt.lineno, *(d.lineno for d in decorators)])


def _class_header(stmt: ast.ClassDef) -> ast.ClassDef:
    """A copy of the class with its methods removed: what the header node covers."""
    return ast.ClassDef(
        name=stmt.name,
        bases=stmt.bases,
        keywords=stmt.keywords,
        body=[m for m in stmt.body if not isinstance(m, _DEFS)],
        decorator_list=stmt.decorator_list,
        **({"type_params": stmt.type_params} if hasattr(stmt, "type_params") else {}),
    )


_lines_cache: tuple[str, list[str]] | None = None


def _source_lines(source: str) -> list[str]:
    """The source split the way ast.get_source_segment splits it, once per
    source string (it re-splits the whole file on every call, which made
    hashing a file quadratic in its node count)."""
    global _lines_cache
    if _lines_cache is not None and _lines_cache[0] is source:
        return _lines_cache[1]
    splitter = getattr(ast, "_splitlines_no_ff", None)
    lines = splitter(source) if splitter else source.splitlines(keepends=True)
    _lines_cache = (source, lines)
    return lines


def _segment(source: str, node: ast.AST) -> str:
    """ast.get_source_segment(source, node) (unpadded), on cached lines."""
    try:
        lineno = node.lineno - 1  # type: ignore[attr-defined]
        end_lineno = node.end_lineno - 1  # type: ignore[attr-defined]
        col = node.col_offset  # type: ignore[attr-defined]
        end_col = node.end_col_offset  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return ""
    if end_lineno is None or end_col is None:
        return ""
    lines = _source_lines(source)
    if end_lineno == lineno:
        return lines[lineno].encode()[col:end_col].decode()
    first = lines[lineno].encode()[col:].decode()
    last = lines[end_lineno].encode()[:end_col].decode()
    return first + "".join(lines[lineno + 1:end_lineno]) + last


def _source_material(source: str, stmt: ast.stmt) -> str:
    """The source text a node's "source" hash covers: decorators included,
    and for a class only its header (decorators, name, bases, keywords,
    non-method members)."""
    def seg(n: ast.AST) -> str:
        return _segment(source, n)
    decorators = getattr(stmt, "decorator_list", None) or []
    deco = "".join("@" + seg(d) + "\n" for d in decorators)
    if isinstance(stmt, ast.ClassDef):
        header = _class_header(stmt)
        parts = [f"class {stmt.name}"]
        parts += [seg(b) for b in header.bases]
        parts += [seg(k) for k in header.keywords]
        parts += [seg(m) for m in header.body]
        return deco + "\n".join(parts)
    return deco + seg(stmt)


def _hash(source: str, stmt: ast.stmt, mode: HashMode = "source") -> str:
    """
    "source": SHA256 of the node's source text (decorators included). Any
              textual change counts, comments inside the node too. The
              coordinator's drift checks use this: an agent reasons over text.
    "ast":    SHA256 of the node's normalized AST (no positions), so
              formatting- and comment-only changes don't count. CI consumers
              (lomitus.impact) use this.
    """
    if mode == "ast":
        target = _class_header(stmt) if isinstance(stmt, ast.ClassDef) else stmt
        material = ast.dump(target, include_attributes=False)
    else:
        material = _source_material(source, stmt)
    return hashlib.sha256(material.encode()).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


@functools.lru_cache(maxsize=64)
def _roots_for(project_root: str, env_value: str | None) -> tuple[Path, ...]:
    root = Path(project_root).resolve()
    configured: list[str] = []
    if env_value:
        configured = [p for p in env_value.split(os.pathsep) if p]
    pyproject: dict = {}
    pp = root / "pyproject.toml"
    if pp.is_file():
        try:
            pyproject = tomllib.loads(pp.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            pyproject = {}
    tool = pyproject.get("tool", {}) if isinstance(pyproject, dict) else {}
    if not configured:
        lom = tool.get("lomitus", {})
        if isinstance(lom, dict) and isinstance(lom.get("import_roots"), list):
            configured = [str(p) for p in lom["import_roots"]]
    candidates: list[Path] = []
    if configured:
        candidates = [root / p for p in configured]
    else:
        # setuptools: package-dir = {"" = "src"}
        pkg_dir = tool.get("setuptools", {}).get("package-dir", {}) if isinstance(tool.get("setuptools"), dict) else {}
        if isinstance(pkg_dir, dict) and isinstance(pkg_dir.get(""), str):
            candidates.append(root / pkg_dir[""])
        # hatch: packages = ["src/pkg"] -> import root "src"
        hatch = tool.get("hatch", {})
        wheel = hatch.get("build", {}).get("targets", {}).get("wheel", {}) if isinstance(hatch, dict) else {}
        for p in (wheel.get("packages", []) if isinstance(wheel, dict) else []):
            if isinstance(p, str) and "/" in p.strip("/"):
                candidates.append(root / p.strip("/").rsplit("/", 1)[0])
        # poetry: packages = [{include = "pkg", from = "src"}]
        for p in tool.get("poetry", {}).get("packages", []) if isinstance(tool.get("poetry"), dict) else []:
            if isinstance(p, dict) and isinstance(p.get("from"), str):
                candidates.append(root / p["from"])
        if (root / "src").is_dir():
            candidates.append(root / "src")
        candidates.append(root)
    out: list[Path] = []
    for c in candidates:
        c = c.resolve()
        if c.is_dir() and _is_within(c, root) and c not in out:
            out.append(c)
    return tuple(out) or (root,)


def import_roots(project_root: str) -> tuple[Path, ...]:
    """
    Directories absolute imports resolve against, in order. Configured by
    LOMITUS_IMPORT_ROOTS (os.pathsep-separated, relative to the project root)
    or `[tool.lomitus] import_roots = [...]` in pyproject.toml; otherwise
    detected: setuptools package-dir, hatch wheel packages, poetry packages'
    `from`, a `src/` directory, then the project root itself (so a top-level
    `tests/` package resolves next to a src layout).
    """
    return _roots_for(str(Path(project_root).resolve()), os.environ.get("LOMITUS_IMPORT_ROOTS"))


# Module-name resolution memo. Valid while the file tree doesn't change:
# index_file clears it on entry unless a batch (index_files) holds it open.
_resolve_memo: dict[tuple[str, tuple[Path, ...], Path], str | None] = {}
_memo_held = False


def _resolve_module(module: str, roots: tuple[Path, ...], project_root: Path) -> str | None:
    if not module:
        return None
    key = (module, roots, project_root)
    if key in _resolve_memo:
        return _resolve_memo[key]
    found = _resolve_module_uncached(module, roots, project_root)
    _resolve_memo[key] = found
    return found


def _resolve_module_uncached(module: str, roots: tuple[Path, ...], project_root: Path) -> str | None:
    parts = module.replace(".", "/")
    for r in roots:
        for suffix in (f"{parts}.py", f"{parts}/__init__.py"):
            candidate = r / suffix
            if candidate.is_file():
                real = candidate.resolve()
                if real.is_relative_to(project_root):
                    return str(real)
    return None


def _resolve_absolute(module: str, project_root: Path) -> str | None:
    """Kept for callers of the old single-root resolver."""
    root = Path(project_root).resolve()
    return _resolve_module(module, import_roots(str(root)), root)


def _relative_base(stmt: ast.ImportFrom, origin: Path) -> Path:
    base = origin.parent
    for _ in range((stmt.level or 1) - 1):
        base = base.parent
    return base


def _module_file(path_no_suffix: Path, root: Path) -> str | None:
    for p in (path_no_suffix.with_suffix(".py"), path_no_suffix / "__init__.py"):
        if p.is_file() and _is_within(p, root):
            return str(p.resolve())
    return None


def _resolve_from(
    stmt: ast.ImportFrom, origin: Path, root: Path, roots: tuple[Path, ...]
) -> tuple[str | None, dict[str, str]]:
    """(file of the "from" module, {bound name: file} for names that are
    submodules), e.g. "from pkg import mod" binds mod -> pkg/mod.py."""
    module = stmt.module or ""
    sub: dict[str, str] = {}
    if stmt.level:
        base = _relative_base(stmt, origin)
        target_dir = base / module.replace(".", "/") if module else base
        mod_file = _module_file(target_dir, root) if module else _module_file(base, root)
        for a in stmt.names:
            if a.name == "*":
                continue
            f = _module_file(target_dir / a.name, root)
            if f:
                sub[a.asname or a.name] = f
    else:
        mod_file = _resolve_module(module, roots, root)
        for a in stmt.names:
            if a.name == "*":
                continue
            f = _resolve_module(f"{module}.{a.name}", roots, root)
            if f:
                sub[a.asname or a.name] = f
    if mod_file and mod_file.endswith("__init__.py") and any(a.name == "*" for a in stmt.names):
        # "from pkg import *" imports the submodules a literal __all__ names.
        pkg_dir = Path(mod_file).parent
        for name in _literal_all(mod_file):
            f = _module_file(pkg_dir / name, root)
            if f:
                sub.setdefault(name, f)
    return mod_file, sub


_all_memo: dict[tuple[str, int, int], list[str]] = {}


def _literal_all(module_file: str) -> list[str]:
    """The names in a module's literal __all__ list or tuple, else [].
    Memoized per (path, mtime, size): every "from pkg import *" importer
    would otherwise re-read and re-parse the same __init__.py."""
    try:
        st = Path(module_file).stat()
    except OSError:
        return []
    key = (module_file, st.st_mtime_ns, st.st_size)
    if key not in _all_memo:
        if len(_all_memo) > 4096:
            _all_memo.clear()
        _all_memo[key] = _literal_all_uncached(module_file)
    return _all_memo[key]


def _literal_all_uncached(module_file: str) -> list[str]:
    try:
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return []
    for stmt in tree.body:
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) and isinstance(
            getattr(stmt, "value", None), (ast.List, ast.Tuple)
        ):
            return [e.value for e in stmt.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _string_imports(tree: ast.AST) -> list[str]:
    """Module names passed as string literals to importlib.import_module()
    or __import__()."""
    return _scan_imports(tree)[1]


def resolve_import(
    stmt: ast.Import | ast.ImportFrom,
    from_file: str,
    project_root: str,
) -> list[str]:
    """
    Resolve an import statement to absolute file paths within the project:
    the imported module, plus each "from x import name" name that is itself
    a submodule. Absolute imports resolve against import_roots().
    Returns empty list for stdlib/third-party imports.
    """
    root = Path(project_root).resolve()
    roots = import_roots(str(root))
    origin = Path(from_file).resolve()
    results: list[str] = []

    if isinstance(stmt, ast.ImportFrom):
        mod_file, sub = _resolve_from(stmt, origin, root, roots)
        if mod_file:
            results.append(mod_file)
        results.extend(f for f in sub.values() if f not in results)
    elif isinstance(stmt, ast.Import):
        for alias in stmt.names:
            resolved = _resolve_module(alias.name, roots, root)
            if resolved and resolved not in results:
                results.append(resolved)

    for f in list(results):
        for init in _ancestor_inits(f, root):
            if init not in results:
                results.append(init)
    return [r for r in results if r != str(origin)]


def _ancestor_inits(module_file: str, project_root: Path) -> list[str]:
    """Every package __init__.py Python runs before module_file on import:
    "import a.b.c" runs a/__init__.py and a/b/__init__.py first. Walks up
    while the directory is a package, inside the project."""
    p = Path(module_file)
    d = p.parent.parent if p.name == "__init__.py" else p.parent
    out: list[str] = []
    while d.is_relative_to(project_root) and d != project_root.parent:
        init = d / "__init__.py"
        if not init.is_file():
            break
        out.append(str(init))
        if d == project_root:
            break
        d = d.parent
    return out


_imports_cache: dict[int, tuple[ast.AST, list, list[str]]] = {}


def _scan_imports(tree: ast.AST) -> tuple[list[ast.Import | ast.ImportFrom], list[str]]:
    """(every import statement at any depth, string-literal import targets),
    from one walk of the tree, memoized per tree object."""
    hit = _imports_cache.get(id(tree))
    if hit is not None and hit[0] is tree:
        return hit[1], hit[2]
    stmts: list[ast.Import | ast.ImportFrom] = []
    strings: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            stmts.append(n)
        elif isinstance(n, ast.Call) and n.args:
            func = n.func
            name = _dotted(func) if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name in ("importlib.import_module", "import_module", "__import__"):
                arg = n.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    strings.append(arg.value)
    if len(_imports_cache) > 1_000:
        _imports_cache.clear()
    _imports_cache[id(tree)] = (tree, stmts, strings)
    return stmts, strings


def _import_nodes(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Every import statement at any depth: module level, inside functions,
    under `if TYPE_CHECKING:`, in try/except."""
    return _scan_imports(tree)[0]


def file_imports(tree: ast.AST, from_file: str, project_root: str) -> set[str]:
    """Project files this file imports, at any nesting depth, plus string
    literal importlib.import_module()/__import__() targets."""
    root = Path(project_root).resolve()
    roots = import_roots(str(root))
    origin = str(Path(from_file).resolve())
    targets: set[str] = set()
    for stmt in _import_nodes(tree):
        targets.update(resolve_import(stmt, from_file, project_root))
    for name in _string_imports(tree):
        f = _resolve_module(name, roots, root)
        if f:
            targets.add(f)
            targets.update(_ancestor_inits(f, root))
    targets.discard(origin)
    return targets


def _project_import_targets(
    tree: ast.Module,
    from_file: str,
    project_root: str,
) -> dict[str, tuple[str, str]]:
    """
    Map bare names bound by "from project_module import name [as alias]" (at
    any nesting depth) to (resolved_absolute_file, name) when project_module
    resolves to a file within the project and `name` is not itself a
    submodule. Used by index_file to look up whether an imported symbol is
    already classified "external" in another file's indexed nodes, so effect
    typing propagates across file boundaries, not just within one. A
    function-local binding is treated as file-wide: an over-approximation.
    """
    root = Path(project_root).resolve()
    roots = import_roots(str(root))
    origin = Path(from_file).resolve()
    targets: dict[str, tuple[str, str]] = {}
    for stmt in _import_nodes(tree):
        if not isinstance(stmt, ast.ImportFrom):
            continue
        mod_file, sub = _resolve_from(stmt, origin, root, roots)
        if not mod_file:
            continue
        for alias in stmt.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            if bound in sub:
                continue  # a submodule: qualified calls through it, see _module_bindings
            targets[bound] = (mod_file, alias.name)
    return targets


def _module_bindings(
    tree: ast.Module, from_file: str, project_root: str
) -> tuple[dict[str, str], set[str]]:
    """
    Names bound to project modules, for resolving qualified calls:
      ({bound name: module file}, {top-level package names bound by a plain
      "import a.b.c"}), from imports at any depth:
        import pkg.mod as m      -> m   -> pkg/mod.py
        from pkg import mod      -> mod -> pkg/mod.py
        from . import mod        -> mod -> ./mod.py
        import pkg               -> pkg -> pkg/__init__.py
        import pkg.mod           -> "pkg" in the prefix set: "pkg.mod.fn()"
                                    resolves by its longest module prefix.
    """
    root = Path(project_root).resolve()
    roots = import_roots(str(root))
    origin = Path(from_file).resolve()
    files: dict[str, str] = {}
    prefixes: set[str] = set()
    for stmt in _import_nodes(tree):
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                f = _resolve_module(a.name, roots, root)
                if a.asname:
                    if f:
                        files[a.asname] = f
                else:
                    top = a.name.split(".", 1)[0]
                    if _resolve_module(top, roots, root):
                        prefixes.add(top)
        else:
            _, sub = _resolve_from(stmt, origin, root, roots)
            files.update(sub)
    return files, prefixes


def _resolve_qualified(
    dotted: str,
    files: dict[str, str],
    prefixes: set[str],
    roots: tuple[Path, ...],
    root: Path,
) -> tuple[str, str] | None:
    """(target file, qualname) for a dotted call through a module binding,
    or None. The qualname is "fn" or "Cls.meth"."""
    parts = dotted.split(".")
    if len(parts) < 2:
        return None
    head = parts[0]
    if head in files:
        target = files[head]
        rest = parts[1:]
        # Descend into subpackages: m.sub.fn where m is a package.
        while len(rest) > 1 and target.endswith("__init__.py"):
            nxt = _module_file(Path(target).parent / rest[0], root)
            if not nxt:
                break
            target, rest = nxt, rest[1:]
        return (target, ".".join(rest)) if 1 <= len(rest) <= 2 else None
    if head in prefixes:
        for i in range(len(parts) - 1, 0, -1):
            f = _resolve_module(".".join(parts[:i]), roots, root)
            if f:
                rest = parts[i:]
                return (f, ".".join(rest)) if 1 <= len(rest) <= 2 else None
    return None


def _lookup_kind(conn: sqlite3.Connection, file_path: str, name: str) -> str | None:
    """The kind of the named node; with several nodes of one name (a property
    getter and setter, overloads) "external" if any of them is."""
    rows = conn.execute(
        "SELECT kind FROM nodes WHERE file_path = ? AND name = ?", (file_path, name)
    ).fetchall()
    if not rows:
        return None
    kinds = {r[0] for r in rows}
    return "external" if "external" in kinds else rows[0][0]


_parse_memo: dict[str, tuple[str, ast.Module]] = {}


def _parse(file_path: str) -> tuple[str, ast.Module]:
    # Inside a batch (index_files / lomitus.impact.index_project, which hold
    # the memo) files don't change, so later effect-typing passes reuse the
    # parse. Outside one, always read the file fresh.
    if _memo_held and file_path in _parse_memo:
        return _parse_memo[file_path]
    source = Path(file_path).read_text(encoding="utf-8")
    parsed = (source, ast.parse(source, filename=file_path))
    if _memo_held:
        _parse_memo[file_path] = parsed
    return parsed


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
        _node_id(abs_path, label): _hash(source, stmt)
        for stmt, _cls, label in _labelled_stmts(tree)
    }
    return stored == current


def _kind_of(
    kind_map: dict[str, Literal["structural", "external"]],
    stmt: ast.stmt,
    class_name: str | None,
) -> Literal["structural", "external", "benign"]:
    if isinstance(stmt, ast.ClassDef):
        return "benign"  # the header node; methods carry the effect typing
    name = _name(stmt, class_name)
    if name is None:
        return "benign"
    return kind_map.get(name, "benign")


def _nodes(
    abs_path: str,
    source: str,
    tree: ast.Module,
    kind_map: dict[str, Literal["structural", "external"]],
    hash_mode: HashMode = "source",
) -> list[Node]:
    return [
        Node(
            node_id=_node_id(abs_path, label),
            file_path=abs_path,
            name=_name(stmt, cls),
            kind=_kind_of(kind_map, stmt, cls),
            line_start=_first_line(stmt),
            line_end=stmt.end_lineno or stmt.lineno,
            content_hash=_hash(source, stmt, hash_mode),
        )
        for stmt, cls, label in _labelled_stmts(tree)
    ]


def parse_source(source: str, file_path: str, hash_mode: HashMode = "source") -> list[Node]:
    """parse_file for source text that is not on disk (e.g. a git blob)."""
    tree = ast.parse(source, filename=file_path)
    return _nodes(file_path, source, tree, _classify_module(tree), hash_mode)


def parse_file(file_path: str, hash_mode: HashMode = "source") -> list[Node]:
    """
    Extract all indexable nodes from a Python file, including class methods.
    Effect typing here is same-file only (no conn/project_root to look up
    cross-file classifications) — see index_file for the cross-file pass.
    """
    abs_path = str(Path(file_path).resolve())
    source, tree = _parse(abs_path)
    return _nodes(abs_path, source, tree, _classify_module(tree), hash_mode)


def index_file(file_path: str, project_root: str, conn: sqlite3.Connection) -> None:
    """
    Parse a file and upsert its nodes and outbound edges into the DB.
    Existing rows for this file are replaced atomically — handles additions,
    modifications, and removals in one pass.

    Effect typing (see _classify_module) propagates transitively within this
    file, plus one cross-file hop: for each "from project_module import name"
    binding and each qualified call through a module binding ("mod.fn()"),
    if the target is already indexed and "external", this file's callers are
    seeded external too. Kept in sync on later changes via update_file's
    dependent re-indexing.
    """
    if not _memo_held:
        _resolve_memo.clear()
    abs_path = str(Path(file_path).resolve())
    source, tree = _parse(abs_path)
    root = Path(project_root).resolve()
    roots = import_roots(str(root))

    aliases = import_aliases(tree)
    import_targets = _project_import_targets(tree, abs_path, project_root)
    extra_external = frozenset(
        name
        for name, (target_file, target_name) in import_targets.items()
        if _lookup_kind(conn, target_file, target_name) == "external"
    )
    files, prefixes = _module_bindings(tree, abs_path, project_root)
    external_qualified: set[str] = set()
    # Files reached by a resolved qualified call are edges too: "import pkg;
    # pkg.mod.fn()" depends on pkg/mod.py without importing it by name, and
    # effect typing looks there, so dependency tracking must as well.
    qualified_targets: set[str] = set()
    if files or prefixes:
        for stmt, _cls in _all_stmts(tree):
            for dotted in _calls(stmt).dotted:
                hit = _resolve_qualified(dotted, files, prefixes, roots, root)
                if not hit:
                    continue
                qualified_targets.add(hit[0])
                if isinstance(stmt, _DEFS) and _lookup_kind(conn, hit[0], hit[1]) == "external":
                    external_qualified.add(dotted)
    kind_map = _classify_module(
        tree,
        aliases=aliases,
        extra_external=extra_external,
        external_qualified=frozenset(external_qualified),
    )

    nodes = _nodes(abs_path, source, tree, kind_map)
    targets = file_imports(tree, abs_path, project_root)
    for t in qualified_targets:
        targets.add(t)
        targets.update(_ancestor_inits(t, root))
    targets.discard(abs_path)
    edges = [(abs_path, target) for target in sorted(targets)]

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


# Conservative batch size for IN-clause queries: SQLite's bound-parameter limit
# (SQLITE_MAX_VARIABLE_NUMBER) is 999 on builds compiled before 3.32.0 and much
# higher on newer ones. Chunking at 500 stays well under either.
_QUERY_BATCH_SIZE = 500


def _fetch_neighbors_batched(conn: sqlite3.Connection, query: str, values: list[str]) -> set[str]:
    """Run `query` (containing one '{}' IN-clause placeholder block) over `values`,
    chunking so the bound-parameter count never exceeds _QUERY_BATCH_SIZE."""
    result: set[str] = set()
    for i in range(0, len(values), _QUERY_BATCH_SIZE):
        chunk = values[i : i + _QUERY_BATCH_SIZE]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(query.format(placeholders), chunk).fetchall()
        result.update(r[0] for r in rows)
    return result


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

    Issues one (batched) query per direction per depth level rather than one
    query per frontier file, so a wide frontier costs O(depth) round trips
    instead of O(depth * frontier size) — the hot path at high concurrent
    agent counts, where every PreToolUse/PostToolUse hook is a fresh process
    hitting the shared coordinator DB.
    """
    start = str(Path(start_file).resolve())
    visited: set[str] = {start}
    frontier: set[str] = {start}

    for _ in range(depth):
        frontier_list = list(frontier)
        next_frontier: set[str] = set()
        if direction in ("forward", "both"):
            next_frontier |= _fetch_neighbors_batched(
                conn, "SELECT to_file FROM edges WHERE from_file IN ({})", frontier_list
            )
        if direction in ("reverse", "both"):
            next_frontier |= _fetch_neighbors_batched(
                conn, "SELECT from_file FROM edges WHERE to_file IN ({})", frontier_list
            )
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
        "SELECT node_id, line_start, line_end, name, kind FROM nodes WHERE file_path = ? ORDER BY line_start",
        (abs_path,),
    ).fetchall()
    if not rows:
        return []

    # node_id -> the edit line ranges that overlap it
    hits: dict[str, list[tuple[int, int]]] = {}
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
            for node_id, line_start, line_end, _name, _kind in rows:
                if line_start <= edit_line_end and line_end >= edit_line_start:
                    hits.setdefault(node_id, []).append((edit_line_start, edit_line_end))
            search_start = offset + 1

    # A class header node spans the whole class, but only its own lines
    # (decorators, the class statement, non-method members) target it: an
    # edit inside a method, or between methods, is not a header edit. Only a
    # class header is a named "benign" node (functions are structural or
    # external), and the file is parsed only when one was hit: the hook runs
    # on every Edit.
    headers = {nid for nid, _s, _e, name, kind in rows if name and kind == "benign" and nid in hits}
    if headers:
        header_lines: dict[str, set[int]] = {}
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        if tree is not None:
            for stmt in tree.body:
                if not isinstance(stmt, ast.ClassDef):
                    continue
                first_body = stmt.body[0].lineno if stmt.body else (stmt.end_lineno or stmt.lineno) + 1
                lines = set(range(_first_line(stmt), max(stmt.lineno, first_body - 1) + 1))
                for m in stmt.body:
                    if not isinstance(m, _DEFS):
                        lines.update(range(_first_line(m), (m.end_lineno or m.lineno) + 1))
                header_lines[_node_id(abs_path, stmt.name)] = lines
        for nid in headers:
            own = header_lines.get(nid)
            if own is not None and not any(own.intersection(range(a, b + 1)) for a, b in hits[nid]):
                del hits[nid]

    matched = set(hits)
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
