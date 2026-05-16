#!/usr/bin/env python3
"""test_coordinator.py — regression harness for v1 coordinator logic. No external deps."""

import sqlite3
import tempfile
from pathlib import Path

from dep_graph import SCHEMA as DEP_SCHEMA, index_file, is_fresh, update_file
from coordinator import (
    _migrate,
    _cascade_trigger,
    _cascade_mark_complete,
    _enqueue_agent,
    _queue_position,
    handle_pretool,
    handle_release,
    handle_revert,
    handle_subagent_stop,
)
from contract import (
    extract_literal_args,
    args_from_annotations,
    is_superset,
    has_external_calls,
)
import ast

PASS = 0
FAIL = 0


def run(name: str, fn) -> None:
    global PASS, FAIL
    try:
        fn()
        print(f"  PASS  {name}")
        PASS += 1
    except Exception as e:
        print(f"  FAIL  {name}: {e}")
        FAIL += 1


def exit_code(fn, *args, **kwargs) -> int:
    try:
        fn(*args, **kwargs)
        return 0
    except SystemExit as e:
        return int(e.code or 0)


def coord_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def dep_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DEP_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_lock_acquisition():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.py"
        f.write_text("def foo(): pass\n")
        conn = coord_db()
        code = exit_code(handle_pretool, str(f), "a1", None, "Edit",
                         {"file_path": str(f), "old_string": "def foo(): pass", "new_string": "def foo(): return 1"},
                         conn)
        assert code == 0, f"expected exit 0, got {code}"
        assert conn.execute("SELECT 1 FROM locks WHERE agent_id='a1'").fetchone(), "lock not acquired"


def test_conflict_block():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "b.py"
        f.write_text("def bar(): pass\n")
        conn = coord_db()
        exit_code(handle_pretool, str(f), "a1", None, "Edit",
                  {"file_path": str(f), "old_string": "def bar(): pass", "new_string": "def bar(): return 1"},
                  conn)
        code = exit_code(handle_pretool, str(f), "a2", None, "Edit",
                         {"file_path": str(f), "old_string": "def bar(): pass", "new_string": "def bar(): return 2"},
                         conn)
        assert code == 2, f"expected exit 2 (block), got {code}"


def test_same_agent_reentry():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "c.py"
        f.write_text("def baz(): pass\n")
        conn = coord_db()
        exit_code(handle_pretool, str(f), "a1", None, "Edit",
                  {"file_path": str(f), "old_string": "def baz(): pass", "new_string": "def baz(): return 1"},
                  conn)
        code = exit_code(handle_pretool, str(f), "a1", None, "Edit",
                         {"file_path": str(f), "old_string": "def baz(): pass", "new_string": "def baz(): return 2"},
                         conn)
        assert code == 0, f"same agent re-entry should be allowed, got {code}"


def test_benign_blocked_when_structural_lock_held():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "d.py"
        f.write_text("import os\ndef qux(): pass\n")
        conn = coord_db()
        exit_code(handle_pretool, str(f), "a1", None, "Edit",
                  {"file_path": str(f), "old_string": "def qux(): pass", "new_string": "def qux(): return 1"},
                  conn)
        code = exit_code(handle_pretool, str(f), "a2", None, "Edit",
                         {"file_path": str(f), "old_string": "import os", "new_string": "import os, sys"},
                         conn)
        assert code == 2, f"benign edit should be blocked by structural lock, got {code}"


def test_benign_allowed_no_lock():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "e.py"
        f.write_text("import os\ndef quux(): pass\n")
        conn = coord_db()
        code = exit_code(handle_pretool, str(f), "a1", None, "Edit",
                         {"file_path": str(f), "old_string": "import os", "new_string": "import os, sys"},
                         conn)
        assert code == 0, f"benign edit with no lock should be allowed, got {code}"
        assert not conn.execute("SELECT 1 FROM locks").fetchone(), "no lock should be held"


def test_release_clears_lock():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "f.py"
        f.write_text("def zzz(): pass\n")
        conn = coord_db()
        exit_code(handle_pretool, str(f), "a1", None, "Edit",
                  {"file_path": str(f), "old_string": "def zzz(): pass", "new_string": "def zzz(): return 0"},
                  conn)
        f.write_text("def zzz(): return 0\n")  # simulate edit landing on disk
        code = exit_code(handle_release, str(f), "a1", conn)
        assert code == 0, f"release should exit 0, got {code}"
        assert not conn.execute("SELECT 1 FROM locks WHERE agent_id='a1'").fetchone(), "lock not released"


def test_subagent_stop_releases_locks():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "g.py"
        f.write_text("def wow(): pass\n")
        conn = coord_db()
        exit_code(handle_pretool, str(f), "a99", None, "Edit",
                  {"file_path": str(f), "old_string": "def wow(): pass", "new_string": "def wow(): return 0"},
                  conn)
        assert conn.execute("SELECT 1 FROM locks WHERE agent_id='a99'").fetchone()
        exit_code(handle_subagent_stop, {"agent_id": "a99"}, conn)
        assert not conn.execute("SELECT 1 FROM locks WHERE agent_id='a99'").fetchone(), "lock not released by subagent_stop"


def test_structural_deletion_propagation():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        lib  = d / "lib.py"
        main = d / "main.py"
        lib.write_text("def foo(): pass\ndef bar(): pass\n")
        main.write_text("from lib import foo\ndef use(): foo()\n")
        conn = dep_db()
        index_file(str(lib), str(d), conn)
        index_file(str(main), str(d), conn)

        nodes_before = conn.execute(
            "SELECT node_id FROM nodes WHERE file_path=?", (str(lib.resolve()),)
        ).fetchall()
        assert len(nodes_before) == 2, f"expected 2 nodes, got {len(nodes_before)}"

        lib.write_text("def foo(): pass\n")  # delete bar
        update_file(str(lib), str(d), conn)

        nodes_after = conn.execute(
            "SELECT node_id FROM nodes WHERE file_path=?", (str(lib.resolve()),)
        ).fetchall()
        assert len(nodes_after) == 1, f"expected 1 node after deletion, got {len(nodes_after)}"
        assert conn.execute(
            "SELECT 1 FROM nodes WHERE file_path=?", (str(main.resolve()),)
        ).fetchone(), "main.py should have been re-indexed after lib deletion"


def test_is_fresh():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "fresh.py"
        f.write_text("def hello(): pass\n")
        conn = dep_db()
        index_file(str(f), d, conn)
        assert is_fresh(str(f), conn), "should be fresh after indexing"
        f.write_text("def hello(): return 42\n")
        assert not is_fresh(str(f), conn), "should be stale after file change"


# ---------------------------------------------------------------------------
# v2 tests
# ---------------------------------------------------------------------------

def test_extract_literal_args():
    caller = "result = add(1, 2)\n"
    args, kwargs = extract_literal_args("add", caller)
    assert args == [1, 2], f"expected [1, 2], got {args}"
    assert kwargs == {}


def test_extract_literal_args_returns_none_for_variables():
    caller = "result = add(x, y)\n"
    result = extract_literal_args("add", caller)
    assert result is None, "should return None when args are not literals"


def test_args_from_annotations():
    src = "def add(a: int, b: int) -> int: return a + b\n"
    tree = ast.parse(src)
    func = tree.body[0]
    args, kwargs = args_from_annotations(func)
    assert args == [0, 0], f"expected [0, 0], got {args}"
    assert kwargs == {}


def test_args_from_annotations_var_placeholder():
    src = "def process(data, count: int): pass\n"
    tree = ast.parse(src)
    func = tree.body[0]
    args, _ = args_from_annotations(func)
    assert args == ["var", 0], f"expected ['var', 0], got {args}"


def test_is_superset_pass_additive():
    required = {"type": "dict", "keys": {"x": {"type": "int"}}}
    actual   = {"type": "dict", "keys": {"x": {"type": "int"}, "y": {"type": "str"}}}
    assert is_superset(required, actual), "additive key should pass"


def test_is_superset_fail_missing_key():
    required = {"type": "dict", "keys": {"x": {"type": "int"}, "y": {"type": "str"}}}
    actual   = {"type": "dict", "keys": {"x": {"type": "int"}}}
    assert not is_superset(required, actual), "missing key should fail"


def test_is_superset_fail_type_change():
    required = {"type": "int"}
    actual   = {"type": "str"}
    assert not is_superset(required, actual), "type change should fail"


def test_is_superset_ndarray_shape():
    required = {"type": "ndarray", "shape": [3, 3], "dtype": "float64"}
    actual   = {"type": "ndarray", "shape": [3, 4], "dtype": "float64"}
    assert not is_superset(required, actual), "shape mismatch should fail"

    actual_ok = {"type": "ndarray", "shape": [3, 3], "dtype": "float64"}
    assert is_superset(required, actual_ok), "matching ndarray should pass"


def test_has_external_calls():
    src_ext = "def fetch(): return requests.get('http://example.com')\n"
    src_plain = "def add(a, b): return a + b\n"
    tree_ext   = ast.parse(src_ext)
    tree_plain = ast.parse(src_plain)
    assert has_external_calls(tree_ext.body[0]),   "requests.get should be external"
    assert not has_external_calls(tree_plain.body[0]), "pure function should not be external"


def test_external_node_classified_correctly():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "api.py"
        f.write_text("import requests\ndef fetch(): return requests.get('http://x.com')\n")
        conn = dep_db()
        index_file(str(f), d, conn)
        row = conn.execute("SELECT kind FROM nodes WHERE name = 'fetch'").fetchone()
        assert row and row[0] == "external", f"expected external, got {row}"


def test_lock_queue_ordering():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "q.py"
        f.write_text("def foo(): pass\n")
        conn = coord_db()
        index_file(str(f), str(d), conn)
        rows = conn.execute("SELECT node_id FROM nodes WHERE file_path = ?", (str(Path(f).resolve()),)).fetchall()
        assert rows
        nid = rows[0][0]

        _enqueue_agent(nid, "agent-low",  None,           conn)
        _enqueue_agent(nid, "agent-high", "orchestrator", conn)

        # orchestrator should be at position 0 despite enqueuing second
        pos_high = _queue_position(nid, "agent-high", conn)
        pos_low  = _queue_position(nid, "agent-low",  conn)
        assert pos_high == 0, f"orchestrator should be front, got position {pos_high}"
        assert pos_low  == 1, f"low-priority should be behind, got position {pos_low}"


def test_cascade_trigger_and_completion():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        lib  = d / "lib.py"
        main = d / "main.py"
        lib.write_text("def compute(): return 1\n")
        main.write_text("from lib import compute\ndef run(): return compute()\n")
        conn = coord_db()
        index_file(str(lib),  str(d), conn)
        index_file(str(main), str(d), conn)

        cascade_nodes = _cascade_trigger(str(lib), str(lib.resolve()) + "::compute",
                                         "a1", None, conn)
        assert cascade_nodes, "cascade should find dependent nodes in main.py"

        # Cascade locks should exist for the dependent nodes
        for nid in cascade_nodes:
            row = conn.execute("SELECT agent_id FROM locks WHERE node_id = ?", (nid,)).fetchone()
            assert row and row[0] == "a1", f"cascade lock missing for {nid}"

        # Mark all cascade nodes complete — cascade should finish
        done = False
        for nid in cascade_nodes:
            done = _cascade_mark_complete(nid, "a1", conn)
        assert done, "cascade should be marked complete after all nodes done"

        row = conn.execute("SELECT status FROM cascades WHERE agent_id = 'a1'").fetchone()
        assert row and row[0] == "complete", f"expected complete, got {row}"


def test_voluntary_revert():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        lib  = d / "lib.py"
        main = d / "main.py"
        original = "def compute(): return 1\n"
        lib.write_text(original)
        main.write_text("from lib import compute\ndef run(): return compute()\n")
        conn = coord_db()
        index_file(str(lib),  str(d), conn)
        index_file(str(main), str(d), conn)

        # Trigger a cascade (stores snapshot of lib.py original content)
        lib_abs = str(lib.resolve())
        nid = conn.execute("SELECT node_id FROM nodes WHERE file_path = ? AND name = 'compute'",
                           (lib_abs,)).fetchone()[0]
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (node_id, file_path, source_content) VALUES (?, ?, ?)",
                (nid, lib_abs, original),
            )
        cascade_nodes = _cascade_trigger(str(lib), nid, "a1", None, conn)
        assert cascade_nodes

        # Simulate the agent changing lib.py
        lib.write_text("def compute(): return 99\n")

        # Voluntary revert
        exit_code(handle_revert, {"agent_id": "a1"}, conn)

        # lib.py should be back to original content
        assert lib.read_text() == original, "file should be reverted to original content"

        # Cascade should be marked reverted
        row = conn.execute("SELECT status FROM cascades WHERE agent_id = 'a1'").fetchone()
        assert row and row[0] == "reverted", f"expected reverted, got {row}"

        # Cascade locks should be released
        for nid in cascade_nodes:
            row = conn.execute("SELECT 1 FROM locks WHERE node_id = ?", (nid,)).fetchone()
            assert not row, f"lock should be released after revert for {nid}"


def test_watchdog_ttl_revert():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        f = Path(d) / "w.py"
        original = "def slow(): pass\n"
        f.write_text(original)
        conn = coord_db()

        # Insert a lock that is already expired
        abs_f = str(f.resolve())
        nid = f"{abs_f}::slow"
        with conn:
            conn.execute(
                "INSERT INTO nodes (node_id, file_path, name, kind, line_start, line_end, content_hash) "
                "VALUES (?, ?, 'slow', 'structural', 1, 1, 'abc')",
                (nid, abs_f),
            )
            conn.execute(
                "INSERT INTO locks (node_id, file_path, agent_id, expires_at) "
                "VALUES (?, ?, 'crashed-agent', datetime('now', '-1 seconds'))",
                (nid, abs_f),
            )
            conn.execute(
                "INSERT INTO snapshots (node_id, file_path, source_content) VALUES (?, ?, ?)",
                (nid, abs_f, original),
            )

        # Simulate agent changed the file before crashing
        f.write_text("def slow(): return 'mutated'\n")

        from watchdog import _sweep
        _sweep(conn)

        # File should be reverted
        assert f.read_text() == original, "watchdog should have reverted the file"
        # Lock should be released
        assert not conn.execute("SELECT 1 FROM locks WHERE node_id = ?", (nid,)).fetchone()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_lock_acquisition,
    test_conflict_block,
    test_same_agent_reentry,
    test_benign_blocked_when_structural_lock_held,
    test_benign_allowed_no_lock,
    test_release_clears_lock,
    test_subagent_stop_releases_locks,
    test_structural_deletion_propagation,
    test_is_fresh,
    # v2
    test_extract_literal_args,
    test_extract_literal_args_returns_none_for_variables,
    test_args_from_annotations,
    test_args_from_annotations_var_placeholder,
    test_is_superset_pass_additive,
    test_is_superset_fail_missing_key,
    test_is_superset_fail_type_change,
    test_is_superset_ndarray_shape,
    test_has_external_calls,
    test_external_node_classified_correctly,
    test_lock_queue_ordering,
    test_cascade_trigger_and_completion,
    test_voluntary_revert,
    test_watchdog_ttl_revert,
]

if __name__ == "__main__":
    for t in TESTS:
        run(t.__name__, t)
    print(f"\n{PASS}/{PASS + FAIL} passed")
    if FAIL:
        raise SystemExit(1)
