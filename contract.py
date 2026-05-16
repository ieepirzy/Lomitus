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
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

# Modules whose presence in a function body marks the node as "external".
# External nodes are locked but skipped for I/O snapshot execution.
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


# ---------------------------------------------------------------------------
# External call detection (used by dep_graph._classify)
# ---------------------------------------------------------------------------

def _call_root(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_root(node.value)
    return None


def has_external_calls(stmt: ast.stmt) -> bool:
    """Return True if a function/class body calls any known external module."""
    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call):
            root = _call_root(node.func)
            if root and root in EXTERNAL_CALL_PREFIXES:
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
    Returns (args, kwargs) or None if no resolvable call site exists.
    """
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
        if name != func_name:
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


def args_from_annotations(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list, dict]:
    """
    Construct call arguments from type annotations.
    Falls back to the "var" placeholder for unannotated params — the executor
    substitutes None for "var" so the function can still be called.
    """
    args: list[Any] = []
    for arg in func_node.args.args:
        if arg.arg in ("self", "cls"):
            continue
        ann = arg.annotation
        if ann is None:
            args.append("var")
        elif isinstance(ann, ast.Name) and ann.id in ANNOTATION_DEFAULTS:
            args.append(ANNOTATION_DEFAULTS[ann.id])
        else:
            args.append("var")
    return args, {}


# ---------------------------------------------------------------------------
# Subprocess execution and output structure capture
# ---------------------------------------------------------------------------

_SNAPSHOT_SCRIPT = textwrap.dedent("""\
    import sys, json
    sys.path.insert(0, {project_root!r})
    sys.path.insert(0, {file_dir!r})
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_mod", {file_path!r})
    _mod  = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _fn   = getattr(_mod, {func_name!r})
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


def _args_repr(args: list) -> str:
    """Render args as Python literal expressions for embedding in the snapshot script."""
    return ", ".join("None" if a == "var" else repr(a) for a in args)


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
    script = _SNAPSHOT_SCRIPT.format(
        project_root=project_root,
        file_dir=str(Path(file_path).parent),
        file_path=str(file_path),
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
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout.strip())
    except Exception:
        return None


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
) -> tuple[list | None, dict | None]:
    """
    Full pipeline: find args (literal call sites → annotations → give up),
    execute in subprocess, return (args, io_snapshot).
    Returns (None, None) if capture is not possible.
    """
    if not func_name:
        return None, None

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
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                    args, _ = args_from_annotations(node)
                    break
        except Exception:
            pass

    if args is None:
        return None, None

    snapshot = take_snapshot(file_path, func_name, args, project_root)
    return args, snapshot
