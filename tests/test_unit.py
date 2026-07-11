#!/usr/bin/env python3
"""
tests/test_unit.py — in-process unit tests for coordinator modules.

Runs entirely in-memory (no subprocess, no real DB file, no git).
Covers: lock enforcement, dep graph, contract system, priority queue,
TTL crash recovery, FileChanged unlink, PostToolBatch drift detection.
"""
from __future__ import annotations

import ast
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from lomitus.contract import (
    args_from_annotations,
    extract_literal_args,
    has_external_calls,
    import_aliases,
    is_superset,
)
from lomitus.coordinator import (
    _cascade_mark_complete,
    _cascade_trigger,
    _enqueue_agent,
    _expire_stale_locks,
    _queue_position,
    handle_file_changed,
    handle_post_tool_batch,
    handle_pretool,
    handle_release,
    handle_revert,
    handle_subagent_start,
    handle_subagent_stop,
    _migrate,
)
from lomitus.dep_graph import SCHEMA as DEP_SCHEMA, index_file, is_fresh, update_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coord_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def _dep_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DEP_SCHEMA)
    return conn


def _exit(fn, *args, **kwargs) -> int:
    try:
        fn(*args, **kwargs)
        return 0
    except SystemExit as e:
        return int(e.code or 0)


def _pretool(conn, f, agent, old, new, agent_type=None):
    return _exit(
        handle_pretool, str(f), agent, agent_type, "Edit",
        {"file_path": str(f), "old_string": old, "new_string": new},
        conn,
    )


# ---------------------------------------------------------------------------
# Lock enforcement
# ---------------------------------------------------------------------------

class TestLockEnforcement(unittest.TestCase):

    def test_lock_acquisition(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.py"
            f.write_text("def foo(): pass\n")
            conn = _coord_db()
            code = _pretool(conn, f, "a1", "def foo(): pass", "def foo(): return 1")
            self.assertEqual(code, 0)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM locks WHERE agent_id='a1'").fetchone())

    def test_conflict_block(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "b.py"
            f.write_text("def bar(): pass\n")
            conn = _coord_db()
            _pretool(conn, f, "a1", "def bar(): pass", "def bar(): return 1")
            code = _pretool(conn, f, "a2", "def bar(): pass", "def bar(): return 2")
            self.assertEqual(code, 2)

    def test_same_agent_reentry(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.py"
            f.write_text("def baz(): pass\n")
            conn = _coord_db()
            _pretool(conn, f, "a1", "def baz(): pass", "def baz(): return 1")
            code = _pretool(conn, f, "a1", "def baz(): pass", "def baz(): return 2")
            self.assertEqual(code, 0, "same agent re-entry must be allowed")

    def test_release_clears_lock(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "f.py"
            f.write_text("def zzz(): pass\n")
            conn = _coord_db()
            _pretool(conn, f, "a1", "def zzz(): pass", "def zzz(): return 0")
            f.write_text("def zzz(): return 0\n")
            code = _exit(handle_release, str(f), "a1", conn)
            self.assertEqual(code, 0)
            self.assertIsNone(conn.execute("SELECT 1 FROM locks WHERE agent_id='a1'").fetchone())

    def test_subagent_stop_releases_locks(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "g.py"
            f.write_text("def wow(): pass\n")
            conn = _coord_db()
            _pretool(conn, f, "a99", "def wow(): pass", "def wow(): return 0")
            self.assertIsNotNone(conn.execute("SELECT 1 FROM locks WHERE agent_id='a99'").fetchone())
            _exit(handle_subagent_stop, {"agent_id": "a99"}, conn)
            self.assertIsNone(conn.execute("SELECT 1 FROM locks WHERE agent_id='a99'").fetchone())


# ---------------------------------------------------------------------------
# Benign edits
# ---------------------------------------------------------------------------

class TestBenignEdits(unittest.TestCase):

    def test_benign_blocked_when_structural_lock_held(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "d.py"
            f.write_text("import os\ndef qux(): pass\n")
            conn = _coord_db()
            _pretool(conn, f, "a1", "def qux(): pass", "def qux(): return 1")
            code = _pretool(conn, f, "a2", "import os", "import os, sys")
            self.assertEqual(code, 2, "benign edit must be blocked by structural lock")

    def test_benign_allowed_no_lock(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "e.py"
            f.write_text("import os\ndef quux(): pass\n")
            conn = _coord_db()
            code = _pretool(conn, f, "a1", "import os", "import os, sys")
            self.assertEqual(code, 0, "benign edit with no locks must be allowed")
            self.assertIsNone(conn.execute("SELECT 1 FROM locks").fetchone(), "no lock should be held")


# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------

class TestDepGraph(unittest.TestCase):

    def test_structural_deletion_propagation(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib  = d / "lib.py"
            main = d / "main.py"
            lib.write_text("def foo(): pass\ndef bar(): pass\n")
            main.write_text("from lib import foo\ndef use(): foo()\n")
            conn = _dep_db()
            index_file(str(lib), str(d), conn)
            index_file(str(main), str(d), conn)

            nodes_before = conn.execute(
                "SELECT node_id FROM nodes WHERE file_path=?", (str(lib.resolve()),)
            ).fetchall()
            self.assertEqual(len(nodes_before), 2)

            lib.write_text("def foo(): pass\n")
            update_file(str(lib), str(d), conn)

            nodes_after = conn.execute(
                "SELECT node_id FROM nodes WHERE file_path=?", (str(lib.resolve()),)
            ).fetchall()
            self.assertEqual(len(nodes_after), 1)
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM nodes WHERE file_path=?", (str(main.resolve()),)).fetchone(),
                "main.py must be re-indexed after lib deletion",
            )

    def test_is_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "fresh.py"
            f.write_text("def hello(): pass\n")
            conn = _dep_db()
            index_file(str(f), d, conn)
            self.assertTrue(is_fresh(str(f), conn))
            f.write_text("def hello(): return 42\n")
            self.assertFalse(is_fresh(str(f), conn))

    def test_external_node_classified(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ext.py"
            f.write_text("import requests\ndef fetch(): return requests.get('http://x.com')\n")
            conn = _dep_db()
            index_file(str(f), d, conn)
            row = conn.execute("SELECT kind FROM nodes WHERE name='fetch'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "external")

    def test_aliased_import_classified_external(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "aliased.py"
            f.write_text("import httpx as http\ndef fetch(): return http.get('http://x.com')\n")
            conn = _dep_db()
            index_file(str(f), d, conn)
            row = conn.execute("SELECT kind FROM nodes WHERE name='fetch'").fetchone()
            self.assertEqual(row[0], "external", "aliased import must resolve to its root module")

    def test_from_import_classified_external(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "fromimp.py"
            f.write_text("from urllib import request\ndef fetch(): return request.urlopen('http://x.com')\n")
            conn = _dep_db()
            index_file(str(f), d, conn)
            row = conn.execute("SELECT kind FROM nodes WHERE name='fetch'").fetchone()
            self.assertEqual(row[0], "external", "from-import must resolve to its root module")

    def test_transitive_same_file_call_classified_external(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "transitive.py"
            f.write_text(
                "import requests\n"
                "def _fetch(): return requests.get('http://x.com')\n"
                "def wrapper(): return _fetch()\n"
            )
            conn = _dep_db()
            index_file(str(f), d, conn)
            row = conn.execute("SELECT kind FROM nodes WHERE name='wrapper'").fetchone()
            self.assertEqual(row[0], "external", "caller of an external function must itself be external")

    def test_transitive_self_call_classified_external(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "method.py"
            f.write_text(
                "import requests\n"
                "class Client:\n"
                "    def _fetch(self): return requests.get('http://x.com')\n"
                "    def wrapper(self): return self._fetch()\n"
            )
            conn = _dep_db()
            index_file(str(f), d, conn)
            row = conn.execute("SELECT kind FROM nodes WHERE name='Client.wrapper'").fetchone()
            self.assertEqual(row[0], "external", "self.-call to an external method must itself be external")

    def test_pure_function_stays_structural(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "pure.py"
            f.write_text("def add(a, b): return a + b\ndef wrapper(): return add(1, 2)\n")
            conn = _dep_db()
            index_file(str(f), d, conn)
            row = conn.execute("SELECT kind FROM nodes WHERE name='wrapper'").fetchone()
            self.assertEqual(row[0], "structural", "calling a pure function must not mark the caller external")

    def test_cross_file_transitive_call_classified_external(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib = d / "lib.py"
            main = d / "main.py"
            lib.write_text("import requests\ndef helper(): return requests.get('http://x.com')\n")
            main.write_text("from lib import helper\ndef use(): return helper()\n")
            conn = _dep_db()
            index_file(str(lib), str(d), conn)
            index_file(str(main), str(d), conn)
            row = conn.execute("SELECT kind FROM nodes WHERE name='use'").fetchone()
            self.assertEqual(row[0], "external", "caller of a cross-file external import must itself be external")

    def test_cross_file_kind_change_propagates_to_dependents(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib = d / "lib.py"
            main = d / "main.py"
            lib.write_text("def helper(): return 1\n")
            main.write_text("from lib import helper\ndef use(): return helper()\n")
            conn = _dep_db()
            index_file(str(lib), str(d), conn)
            index_file(str(main), str(d), conn)
            row = conn.execute("SELECT kind FROM nodes WHERE name='use'").fetchone()
            self.assertEqual(row[0], "structural")

            lib.write_text("import requests\ndef helper(): return requests.get('http://x.com')\n")
            update_file(str(lib), str(d), conn)

            row = conn.execute("SELECT kind FROM nodes WHERE name='use'").fetchone()
            self.assertEqual(row[0], "external", "dependent must be re-classified after lib's kind changes")


# ---------------------------------------------------------------------------
# Contract system
# ---------------------------------------------------------------------------

class TestContractSystem(unittest.TestCase):

    def test_extract_literal_args(self):
        args, kwargs = extract_literal_args("add", "result = add(1, 2)\n")
        self.assertEqual(args, [1, 2])
        self.assertEqual(kwargs, {})

    def test_extract_literal_args_variables_returns_none(self):
        result = extract_literal_args("add", "result = add(x, y)\n")
        self.assertIsNone(result)

    def test_args_from_annotations(self):
        tree = ast.parse("def add(a: int, b: int) -> int: return a + b\n")
        args, _ = args_from_annotations(tree.body[0])
        self.assertEqual(args, [0, 0])

    def test_args_from_annotations_var_placeholder(self):
        tree = ast.parse("def process(data, count: int): pass\n")
        args, _ = args_from_annotations(tree.body[0])
        self.assertEqual(args, ["var", 0])

    def test_is_superset_additive_passes(self):
        req = {"type": "dict", "keys": {"x": {"type": "int"}}}
        act = {"type": "dict", "keys": {"x": {"type": "int"}, "y": {"type": "str"}}}
        self.assertTrue(is_superset(req, act))

    def test_is_superset_missing_key_fails(self):
        req = {"type": "dict", "keys": {"x": {"type": "int"}, "y": {"type": "str"}}}
        act = {"type": "dict", "keys": {"x": {"type": "int"}}}
        self.assertFalse(is_superset(req, act))

    def test_is_superset_type_change_fails(self):
        self.assertFalse(is_superset({"type": "int"}, {"type": "str"}))

    def test_is_superset_ndarray_shape_mismatch(self):
        req = {"type": "ndarray", "shape": [3, 3], "dtype": "float64"}
        self.assertFalse(is_superset(req, {"type": "ndarray", "shape": [3, 4], "dtype": "float64"}))
        self.assertTrue(is_superset(req,  {"type": "ndarray", "shape": [3, 3], "dtype": "float64"}))

    def test_has_external_calls(self):
        tree_ext   = ast.parse("def fetch(): return requests.get('http://x.com')\n")
        tree_plain = ast.parse("def add(a, b): return a + b\n")
        self.assertTrue(has_external_calls(tree_ext.body[0]))
        self.assertFalse(has_external_calls(tree_plain.body[0]))

    def test_has_external_calls_with_aliases(self):
        tree = ast.parse("def fetch(): return http.get('http://x.com')\n")
        self.assertFalse(has_external_calls(tree.body[0]), "unresolved alias must not false-positive")
        self.assertTrue(has_external_calls(tree.body[0], aliases={"http": "httpx"}))

    def test_import_aliases_resolves_import_as_and_from_import(self):
        tree = ast.parse("import httpx as http\nfrom urllib import request as req\n")
        aliases = import_aliases(tree)
        self.assertEqual(aliases["http"], "httpx")
        self.assertEqual(aliases["req"], "urllib")

    def test_cascade_trigger_and_completion(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib  = d / "lib.py"
            main = d / "main.py"
            lib.write_text("def compute(): return 1\n")
            main.write_text("from lib import compute\ndef run(): return compute()\n")
            conn = _coord_db()
            index_file(str(lib),  str(d), conn)
            index_file(str(main), str(d), conn)

            root_nid = str(lib.resolve()) + "::compute"
            cascade_nodes = _cascade_trigger(str(lib), root_nid, "a1", None, conn)
            self.assertTrue(cascade_nodes, "cascade must find dependents in main.py")

            for nid in cascade_nodes:
                row = conn.execute("SELECT agent_id FROM locks WHERE node_id=?", (nid,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "a1")

            done = False
            for nid in cascade_nodes:
                done = _cascade_mark_complete(nid, "a1", conn)
            self.assertTrue(done)
            row = conn.execute("SELECT status FROM cascades WHERE agent_id='a1'").fetchone()
            self.assertEqual(row[0], "complete")

    def test_voluntary_revert(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib  = d / "lib.py"
            main = d / "main.py"
            original = "def compute(): return 1\n"
            lib.write_text(original)
            main.write_text("from lib import compute\ndef run(): return compute()\n")
            conn = _coord_db()
            index_file(str(lib),  str(d), conn)
            index_file(str(main), str(d), conn)

            lib_abs = str(lib.resolve())
            nid = conn.execute(
                "SELECT node_id FROM nodes WHERE file_path=? AND name='compute'", (lib_abs,)
            ).fetchone()[0]
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO snapshots (node_id, file_path, source_content) VALUES (?,?,?)",
                    (nid, lib_abs, original),
                )
            cascade_nodes = _cascade_trigger(str(lib), nid, "a1", None, conn)
            self.assertTrue(cascade_nodes)

            lib.write_text("def compute(): return 99\n")
            _exit(handle_revert, {"agent_id": "a1"}, conn)

            self.assertEqual(lib.read_text(), original, "file must be reverted")
            row = conn.execute("SELECT status FROM cascades WHERE agent_id='a1'").fetchone()
            self.assertEqual(row[0], "reverted")
            for nid in cascade_nodes:
                self.assertIsNone(conn.execute("SELECT 1 FROM locks WHERE node_id=?", (nid,)).fetchone())


# ---------------------------------------------------------------------------
# Priority queue
# ---------------------------------------------------------------------------

class TestPriorityQueue(unittest.TestCase):

    def test_orchestrator_jumps_queue(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "q.py"
            f.write_text("def foo(): pass\n")
            conn = _coord_db()
            index_file(str(f), str(d), conn)
            nid = conn.execute(
                "SELECT node_id FROM nodes WHERE file_path=?", (str(f.resolve()),)
            ).fetchone()[0]

            _enqueue_agent(nid, "low",  None,           conn)
            _enqueue_agent(nid, "high", "orchestrator", conn)

            self.assertEqual(_queue_position(nid, "high", conn), 0, "orchestrator must be front")
            self.assertEqual(_queue_position(nid, "low",  conn), 1)


# ---------------------------------------------------------------------------
# TTL crash recovery
# ---------------------------------------------------------------------------

class TestTTLRecovery(unittest.TestCase):

    def _setup(self, d: Path, conn, *, with_snapshot: bool):
        f = d / "w.py"
        original = "def slow(): pass\n"
        f.write_text(original)
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
                "VALUES (?, ?, 'crashed', datetime('now', '-1 second'))",
                (nid, abs_f),
            )
            if with_snapshot:
                conn.execute(
                    "INSERT INTO snapshots (node_id, file_path, source_content) VALUES (?, ?, ?)",
                    (nid, abs_f, original),
                )
        return f, nid, original

    def test_expired_lock_released_no_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            conn = _coord_db()
            f, nid, _ = self._setup(d, conn, with_snapshot=False)
            f.write_text("def slow(): return 'mutated'\n")

            _expire_stale_locks(conn)

            self.assertIsNone(conn.execute("SELECT 1 FROM locks WHERE node_id=?", (nid,)).fetchone(),
                              "expired lock must be released")
            # No snapshot → file left as-is
            self.assertEqual(f.read_text(), "def slow(): return 'mutated'\n")

    def test_expired_lock_reverts_file_with_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            conn = _coord_db()
            f, nid, original = self._setup(d, conn, with_snapshot=True)
            f.write_text("def slow(): return 'mutated'\n")

            _expire_stale_locks(conn)

            self.assertIsNone(conn.execute("SELECT 1 FROM locks WHERE node_id=?", (nid,)).fetchone(),
                              "expired lock must be released")
            self.assertEqual(f.read_text(), original, "file must be reverted from snapshot")

    def test_non_expired_lock_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            conn = _coord_db()
            f = d / "live.py"
            f.write_text("def active(): pass\n")
            abs_f = str(f.resolve())
            nid = f"{abs_f}::active"
            with conn:
                conn.execute(
                    "INSERT INTO nodes (node_id, file_path, name, kind, line_start, line_end, content_hash) "
                    "VALUES (?, ?, 'active', 'structural', 1, 1, 'xyz')", (nid, abs_f),
                )
                conn.execute(
                    "INSERT INTO locks (node_id, file_path, agent_id, expires_at) "
                    "VALUES (?, ?, 'alive', datetime('now', '+60 seconds'))",
                    (nid, abs_f),
                )

            _expire_stale_locks(conn)

            self.assertIsNotNone(conn.execute("SELECT 1 FROM locks WHERE node_id=?", (nid,)).fetchone(),
                                 "non-expired lock must not be touched")


# ---------------------------------------------------------------------------
# FileChanged unlink
# ---------------------------------------------------------------------------

class TestFileChangedUnlink(unittest.TestCase):

    def test_unlink_purges_edges_and_nodes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib  = d / "lib.py"
            main = d / "main.py"
            lib.write_text("def helper(): pass\n")
            main.write_text("from lib import helper\ndef use(): helper()\n")
            conn = _coord_db()
            index_file(str(lib),  str(d), conn)
            index_file(str(main), str(d), conn)

            edge = conn.execute(
                "SELECT 1 FROM edges WHERE from_file=? AND to_file=?",
                (str(main.resolve()), str(lib.resolve())),
            ).fetchone()
            self.assertIsNotNone(edge, "edge main→lib must exist before unlink")

            lib.unlink()
            _exit(handle_file_changed, {"file_path": str(lib), "event": "unlink"}, conn)

            self.assertIsNone(
                conn.execute("SELECT 1 FROM nodes WHERE file_path=?", (str(lib.resolve()),)).fetchone(),
                "lib nodes must be purged after unlink",
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM edges WHERE to_file=?", (str(lib.resolve()),)
                ).fetchone(),
                "edges pointing to deleted file must be purged",
            )

    def test_change_event_warms_cache(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            f = d / "warm.py"
            f.write_text("def thing(): pass\n")
            conn = _coord_db()

            _exit(handle_file_changed, {"file_path": str(f), "event": "change"}, conn)

            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM nodes WHERE file_path=?", (str(f.resolve()),)).fetchone(),
                "change event must index the file into the cache",
            )


# ---------------------------------------------------------------------------
# PostToolBatch drift detection
# ---------------------------------------------------------------------------

class TestPostToolBatch(unittest.TestCase):

    def test_no_drift_passes(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "batch.py"
            f.write_text("def compute(): return 1\n")
            conn = _coord_db()
            _pretool(conn, f, "agent-b", "def compute(): return 1", "def compute(): return 2")

            code = _exit(handle_post_tool_batch, {"agent_id": "agent-b"}, conn)
            self.assertEqual(code, 0, "no drift must pass PostToolBatch")

    def test_drift_detected_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "drift.py"
            f.write_text("def compute(): return 1\n")
            conn = _coord_db()
            _pretool(conn, f, "agent-d", "def compute(): return 1", "def compute(): return 2")

            # Simulate another agent mutating the node hash in the canonical DB
            # while agent-d holds its lock (drift scenario).
            with conn:
                conn.execute(
                    "UPDATE nodes SET content_hash='DIFFERENT' WHERE file_path=?",
                    (str(Path(f).resolve()),),
                )

            code = _exit(handle_post_tool_batch, {"agent_id": "agent-d"}, conn)
            self.assertEqual(code, 2, "drift must cause PostToolBatch to block")

    def test_no_locks_no_op(self):
        conn = _coord_db()
        code = _exit(handle_post_tool_batch, {"agent_id": "nobody"}, conn)
        self.assertEqual(code, 0, "agent with no locks must pass trivially")


# ---------------------------------------------------------------------------
# SubagentStart — only registers agent, not worktree
# ---------------------------------------------------------------------------

class TestSubagentStart(unittest.TestCase):

    def test_registers_agent_only(self):
        conn = _coord_db()
        _exit(handle_subagent_start,
              {"agent_id": "sa-1", "agent_type": "worker", "session_id": "sess-1"},
              conn)

        row = conn.execute("SELECT agent_type FROM agents WHERE agent_id='sa-1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "worker")

        # Worktrees table must NOT get a row — that's WorktreeCreate's job.
        wt_row = conn.execute("SELECT 1 FROM worktrees WHERE agent_id='sa-1'").fetchone()
        self.assertIsNone(wt_row, "SubagentStart must not insert into worktrees")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
