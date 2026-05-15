#!/usr/bin/env python3
"""test_coordinator.py — regression harness for v1 coordinator logic. No external deps."""

import sqlite3
import tempfile
from pathlib import Path

from dep_graph import SCHEMA as DEP_SCHEMA, index_file, is_fresh, update_file
from coordinator import (
    _migrate,
    handle_pretool,
    handle_release,
    handle_subagent_stop,
)

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
]

if __name__ == "__main__":
    for t in TESTS:
        run(t.__name__, t)
    print(f"\n{PASS}/{PASS + FAIL} passed")
    if FAIL:
        raise SystemExit(1)
