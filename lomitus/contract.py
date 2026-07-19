#!/usr/bin/env python3
"""
contract.py — I/O contract extraction, execution, and validation for the v2 coordinator.

Captures the input/output structure of target functions at lock acquisition and
validates at PostToolUse that the post-edit output is a superset of the pre-edit
structure (open-world assumption: additive changes pass, subtractive/mutating changes
trigger the cascade flow).
"""
from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Modules whose presence in a function body marks the node as "external".
# External nodes are locked but skipped for I/O snapshot execution.
#
# Matching against this set alone only catches *direct* calls with the
# literal module name in scope. dep_graph._classify_module builds on top of
# has_external_calls()/import_aliases() below to also catch:
#   - aliased/from-imports ("import httpx as http", "from urllib import request")
#     via import_aliases()
#   - transitive same-file calls (a "pure"-looking function that calls a
#     local helper which itself does a network/db call) via the call-graph
#     propagation in dep_graph._classify_module
#   - the same propagation across a resolved "from project_module import name"
#     edge, via dep_graph.index_file's cross-file DB lookup
# It does not attempt to resolve dynamic dispatch, qualified module calls
# through a project-internal alias (e.g. "import mymod; mymod.fn()"), or
# decorators/wrappers — those remain conservatively "structural" unless a
# direct or transitively-reachable call matches this list.
EXTERNAL_CALL_PREFIXES: frozenset[str] = frozenset({
    "requests", "httpx", "aiohttp", "urllib", "urllib3",
    "socket", "smtplib", "ftplib",
    "psycopg2", "asyncpg", "pymysql", "sqlalchemy",
    "pymongo", "redis", "elasticsearch",
    "boto3", "botocore",
    "openai", "anthropic",
    "subprocess",
})

# Type annotation name → safe default value for constructing test args.
ANNOTATION_DEFAULTS: dict[str, Any] = {
    "int": 0, "float": 0.0, "str": "", "bool": False,
    "list": [], "dict": {}, "tuple": (), "set": set(),
    "bytes": b"",
}

_UNRESOLVABLE = object()  # sentinel for args we cannot resolve statically


class SyntheticMock:
    """
    Stand-in object for a complex-type parameter (dataclass, Pydantic model,
    SQLAlchemy ORM object, or any other project-defined class) whose real
    shape we cannot construct statically. Attribute access falls back to
    None instead of raising, so a function that merely reads through the
    mock during snapshot execution does not blow up on a missing field.
    """

    def __getattr__(self, item: str) -> Any:
        return None


@dataclass(frozen=True)
class SnapshotResult:
    """Result of snapshot execution, including why validation was unavailable."""

    status: str
    snapshot: dict | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# External call detection (used by dep_graph._classify_module)
# ---------------------------------------------------------------------------

def _call_root(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_root(node.value)
    return None


def import_aliases(tree: ast.Module) -> dict[str, str]:
    """
    Map names bound by this module's top-level imports to their root module
    name, so aliased/from-imports resolve the same as a direct import:
      "import httpx as http"        -> {"http": "httpx"}
      "from urllib import request"  -> {"request": "urllib"}
      "import urllib.request as r"  -> {"r": "urllib"}
    Only module-level imports are considered — a function-local "import x"
    only binds within that function and is out of scope for this heuristic.
    """
    aliases: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                root = alias.name.split(".")[0]
                bound = (alias.asname or alias.name).split(".")[0]
                aliases[bound] = root
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.level:  # relative import — not an external module
                continue
            root = (stmt.module or "").split(".")[0]
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                aliases[bound] = root
    return aliases


def has_external_calls(stmt: ast.stmt, aliases: dict[str, str] | None = None) -> bool:
    """
    Return True if a function/class body directly calls a known external
    module. 'aliases' (see import_aliases) resolves aliased/from-imported
    names to their root module before matching against EXTERNAL_CALL_PREFIXES.
    This only detects *direct* calls — see dep_graph._classify_module for
    transitive (same-file and cross-file) propagation.
    """
    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    aliases = aliases or {}
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call):
            root = _call_root(node.func)
            if root and aliases.get(root, root) in EXTERNAL_CALL_PREFIXES:
                return True
    return False


# ---------------------------------------------------------------------------
# Call site argument extraction
# ---------------------------------------------------------------------------

def _literal_value(node: ast.expr) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        items = [_literal_value(e) for e in node.elts]
        return _UNRESOLVABLE if any(i is _UNRESOLVABLE for i in items) else items
    if isinstance(node, ast.Tuple):
        items = [_literal_value(e) for e in node.elts]
        return _UNRESOLVABLE if any(i is _UNRESOLVABLE for i in items) else tuple(items)
    if isinstance(node, ast.Dict):
        result = {}
        for k, v in zip(node.keys, node.values):
            kv, vv = _literal_value(k), _literal_value(v)
            if kv is _UNRESOLVABLE or vv is _UNRESOLVABLE:
                return _UNRESOLVABLE
            result[kv] = vv
        return result
    return _UNRESOLVABLE


def extract_literal_args(func_name: str, caller_source: str) -> tuple[list, dict] | None:
    """
    Find the first fully-literal call site for func_name in caller_source.
    func_name may be qualified ("ClassName.method") — matching uses the unqualified
    part so both obj.method(...) and Class.method(...) call sites are found.
    Returns (args, kwargs) or None if no resolvable call site exists.
    """
    unqualified = func_name.split(".")[-1]
    try:
        tree = ast.parse(caller_source)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id if isinstance(node.func, ast.Name)
            else node.func.attr if isinstance(node.func, ast.Attribute)
            else None
        )
        if name != unqualified:
            continue

        args: list[Any] = []
        for arg in node.args:
            v = _literal_value(arg)
            if v is _UNRESOLVABLE:
                args = None  # type: ignore[assignment]
                break
            args.append(v)
        if args is None:
            continue

        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            v = _literal_value(kw.value)
            if v is _UNRESOLVABLE or not kw.arg:
                kwargs = None  # type: ignore[assignment]
                break
            kwargs[kw.arg] = v
        if kwargs is None:
            continue

        return args, kwargs

    return None


def mock_type_from_db(annotation_name: str, conn: sqlite3.Connection) -> Any:
    """
    Resolve a complex-type annotation (a name not in ANNOTATION_DEFAULTS) against
    the nodes table. If annotation_name matches an indexed project-defined class —
    a dataclass, Pydantic model, SQLAlchemy ORM object, or any other class with at
    least one method — return a SyntheticMock in its place so snapshot execution
    has something to call/read instead of None. Falls back to _UNRESOLVABLE when
    no matching class is indexed, same as the pre-existing behavior for
    unresolvable params.

    dep_graph._all_stmts intentionally skips ClassDef itself when indexing —
    only its method members become nodes, stored as "ClassName.method_name"
    (see that function's docstring: a whole-class node would cause false-positive
    drift on every sibling method edit). So a class is never present in this table
    under its own bare name; it is only detectable via a node whose name starts
    with "annotation_name.". GLOB (not LIKE) is used to match that prefix: GLOB's
    wildcards are "*" and "?", neither of which is a legal character in a Python
    identifier, so annotation_name needs no escaping. LIKE's wildcard "_" *is* a
    legal identifier character and would silently mismatch on class names
    containing an underscore.
    """
    row = conn.execute(
        "SELECT 1 FROM nodes WHERE name GLOB ? LIMIT 1",
        (f"{annotation_name}.*",),
    ).fetchone()
    if row is None:
        return _UNRESOLVABLE
    return SyntheticMock()


def args_from_annotations(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    conn: sqlite3.Connection | None = None,
) -> tuple[list, dict]:
    """
    Construct call arguments from type annotations.
    Scalar annotations (see ANNOTATION_DEFAULTS) get a safe default value. Complex-type
    annotations fall back to mock_type_from_db when conn is provided, synthesizing a
    SyntheticMock from an indexed ClassDef instead of giving up outright.
    Falls back to _UNRESOLVABLE for unannotated or unresolvable params — _args_repr maps
    these to None so the function can still be called without a string-sentinel collision.
    """
    args: list[Any] = []
    for arg in func_node.args.args:
        if arg.arg in ("self", "cls"):
            continue
        ann = arg.annotation
        if ann is None:
            args.append(_UNRESOLVABLE)
        elif isinstance(ann, ast.Name) and ann.id in ANNOTATION_DEFAULTS:
            args.append(ANNOTATION_DEFAULTS[ann.id])
        elif isinstance(ann, ast.Name) and conn is not None:
            args.append(mock_type_from_db(ann.id, conn))
        else:
            args.append(_UNRESOLVABLE)
    return args, {}


# ---------------------------------------------------------------------------
# Subprocess execution and output structure capture
# ---------------------------------------------------------------------------

_SNAPSHOT_SCRIPT = textwrap.dedent("""\
    import sys, json, types
    sys.path.insert(0, {project_root!r})
    sys.path.insert(0, {file_dir!r})

    class _SyntheticMock:
        def __getattr__(self, item): return None

    _mod = types.ModuleType("_mod")
    _mod.__file__ = {file_path!r}
    sys.modules["_mod"] = _mod
    exec(compile({source!r}, {file_path!r}, "exec"), _mod.__dict__)
    _qname = {func_name!r}
    if "." in _qname:
        _cls_name, _meth_name = _qname.split(".", 1)
        _cls = getattr(_mod, _cls_name)
        try:
            _fn = getattr(_cls(), _meth_name)
        except Exception:
            _fn = getattr(_cls, _meth_name)
    else:
        _fn = getattr(_mod, _qname)
    _result = _fn(*[{args_repr}])

    def _snap(v):
        if v is None: return {{"type": "NoneType"}}
        if isinstance(v, bool): return {{"type": "bool"}}
        if isinstance(v, int): return {{"type": "int"}}
        if isinstance(v, float): return {{"type": "float"}}
        if isinstance(v, str): return {{"type": "str"}}
        if isinstance(v, (list, tuple)):
            elem = _snap(v[0]) if v else {{"type": "unknown"}}
            return {{"type": type(v).__name__, "element": elem, "len": len(v)}}
        if isinstance(v, dict):
            return {{"type": "dict", "keys": {{k: _snap(vv) for k, vv in v.items()}}}}
        if hasattr(v, "__dataclass_fields__"):
            return {{"type": "dataclass", "cls": type(v).__name__,
                    "fields": {{f: _snap(getattr(v, f)) for f in v.__dataclass_fields__}}}}
        if hasattr(v, "shape") and hasattr(v, "dtype"):
            return {{"type": "ndarray", "shape": list(v.shape), "dtype": str(v.dtype)}}
        return {{"type": type(v).__name__}}

    print(json.dumps(_snap(_result)))
""")


_SAFE_DECORATORS = {"dataclass", "staticmethod", "classmethod", "property"}


def _decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _safe_assignment(stmt: ast.stmt) -> bool:
    value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
    if value is None:
        return False
    try:
        ast.literal_eval(value)
        return True
    except (ValueError, TypeError):
        return False


class _SnapshotSanitizer(ast.NodeTransformer):
    """Remove executable initialization while retaining callable definitions."""

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        node.decorator_list = [
            d for d in node.decorator_list if _decorator_name(d) in _SAFE_DECORATORS
        ]
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        return self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):  # noqa: N802
        return self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef):  # noqa: N802
        node.decorator_list = [
            d for d in node.decorator_list if _decorator_name(d) in _SAFE_DECORATORS
        ]
        body: list[ast.stmt] = []
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visited = self.visit(stmt)
                if visited is not None:
                    body.append(visited)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and _safe_assignment(stmt):
                body.append(stmt)
            elif isinstance(stmt, ast.Pass):
                body.append(stmt)
        node.body = body or [ast.Pass()]
        return node


def isolated_snapshot_source(file_path: str) -> str:
    """Return source with import-time initialization removed.

    Imports are retained because definitions may need them. This prevents the target
    module's top-level initialization from running, but it is not a security sandbox:
    imported modules and the target function itself still execute normally.
    """
    source = Path(file_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=file_path)
    sanitizer = _SnapshotSanitizer()
    kept: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            kept.append(stmt)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            visited = sanitizer.visit(stmt)
            if visited is not None:
                kept.append(visited)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and _safe_assignment(stmt):
            kept.append(stmt)
    tree.body = kept or [ast.Pass()]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _args_repr(args: list) -> str:
    """
    Render args as Python literal expressions for embedding in the snapshot script.
    SyntheticMock instances have no useful repr() of their own — the snapshot script
    defines an equivalent _SyntheticMock class (see _SNAPSHOT_SCRIPT), so a mock arg
    is rendered as a constructor call into that class instead.
    """
    parts = []
    for a in args:
        if a is _UNRESOLVABLE:
            parts.append("None")
        elif isinstance(a, SyntheticMock):
            parts.append("_SyntheticMock()")
        else:
            parts.append(repr(a))
    return ", ".join(parts)


def take_snapshot(
    file_path: str,
    func_name: str,
    args: list,
    project_root: str,
    timeout: int = 5,
) -> dict | None:
    """
    Execute func_name from file_path in a subprocess and return its output structure.
    Returns None on any failure — import error, timeout, external dep missing, etc.
    Callers treat None as "snapshot unavailable, skip contract validation for this node."
    """
    return take_snapshot_result(file_path, func_name, args, project_root, timeout).snapshot


def take_snapshot_result(
    file_path: str,
    func_name: str,
    args: list,
    project_root: str,
    timeout: int = 5,
) -> SnapshotResult:
    """Execute an isolated snapshot and preserve its success/failure reason."""
    try:
        source = isolated_snapshot_source(file_path)
    except (OSError, SyntaxError) as exc:
        return SnapshotResult("rewrite_error", detail=str(exc))

    script = _SNAPSHOT_SCRIPT.format(
        project_root=project_root,
        file_dir=str(Path(file_path).parent),
        file_path=str(file_path),
        source=source,
        func_name=func_name,
        args_repr=_args_repr(args),
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"snapshot process exited {result.returncode}"
            return SnapshotResult("execution_error", detail=detail[-2000:])
        if not result.stdout.strip():
            return SnapshotResult("empty_output", detail="snapshot process produced no output")
        try:
            return SnapshotResult("ok", snapshot=json.loads(result.stdout.strip()))
        except json.JSONDecodeError as exc:
            return SnapshotResult("invalid_output", detail=str(exc))
    except subprocess.TimeoutExpired:
        return SnapshotResult("timeout", detail=f"snapshot exceeded {timeout}s")
    except Exception as exc:
        return SnapshotResult("execution_error", detail=str(exc))


# ---------------------------------------------------------------------------
# Superset validation (open-world assumption)
# ---------------------------------------------------------------------------

def is_superset(required: dict, actual: dict) -> bool:
    """
    Return True if actual output structure is a superset of required.
    Additive changes (new keys, extended output) pass.
    Subtractive or type-mutating changes fail.
    """
    if required.get("type") != actual.get("type"):
        return False
    t = required.get("type")
    if t == "dict":
        for key, req_val in required.get("keys", {}).items():
            if key not in actual.get("keys", {}):
                return False
            if not is_superset(req_val, actual["keys"][key]):
                return False
    elif t in ("list", "tuple"):
        req_elem = required.get("element", {})
        if req_elem.get("type") not in ("unknown", None):
            if not is_superset(req_elem, actual.get("element", {})):
                return False
    elif t == "ndarray":
        if required.get("shape") != actual.get("shape"):
            return False
        if required.get("dtype") != actual.get("dtype"):
            return False
    elif t == "dataclass":
        if required.get("cls") != actual.get("cls"):
            return False
        for field, req_val in required.get("fields", {}).items():
            if field not in actual.get("fields", {}):
                return False
            if not is_superset(req_val, actual["fields"][field]):
                return False
    # Scalars and NoneType: type match above is sufficient.
    return True


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def capture_node_snapshot(
    file_path: str,
    func_name: str | None,
    project_root: str,
    caller_sources: list[str],
    conn: sqlite3.Connection | None = None,
) -> tuple[list | None, dict | None]:
    """
    Full pipeline: find args (literal call sites → annotations → give up),
    execute in subprocess, return (args, io_snapshot).
    Returns (None, None) if capture is not possible.
    conn, when provided, lets annotation-based args resolve complex-type
    params via mock_type_from_db instead of falling back to _UNRESOLVABLE.
    """
    args, result = capture_node_snapshot_result(
        file_path, func_name, project_root, caller_sources, conn
    )
    return args, result.snapshot


def capture_node_snapshot_result(
    file_path: str,
    func_name: str | None,
    project_root: str,
    caller_sources: list[str],
    conn: sqlite3.Connection | None = None,
) -> tuple[list | None, SnapshotResult]:
    """Capture a node snapshot while retaining an explicit outcome status."""
    if not func_name:
        return None, SnapshotResult("not_callable", detail="node has no callable name")

    args: list | None = None
    for src in caller_sources:
        result = extract_literal_args(func_name, src)
        if result is not None:
            args, _ = result
            break

    if args is None:
        try:
            source = Path(file_path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=file_path)
            unqualified = func_name.split(".")[-1]
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == unqualified:
                    args, _ = args_from_annotations(node, conn)
                    break
        except Exception:
            pass

    if args is None:
        return None, SnapshotResult("unresolved_args", detail="no arguments could be synthesized")

    return args, take_snapshot_result(file_path, func_name, args, project_root)
