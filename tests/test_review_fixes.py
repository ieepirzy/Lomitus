"""
tests/test_review_fixes.py — regressions found by an independent review of
the graph-correctness and lomitus.impact changes, each reproduced first.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from lomitus import coordinator as co
from lomitus import dep_graph


def _write(root: Path, rel: str, body: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return str(p.resolve())


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        dep_graph._roots_for.cache_clear()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        os.environ.pop("LOMITUS_IMPORT_ROOTS", None)
        self.addCleanup(env.stop)

    def tearDown(self) -> None:
        dep_graph._roots_for.cache_clear()
        self._tmp.cleanup()


SETTER = """
    class C:
        @property
        def x(self):
            return 1

        @x.setter
        def x(self, v):
            pass

        def y(self):
            return 2
"""


class TestWorktreeStaleCheck(_Tmp):
    """Item 1: the canonical side was keyed by label ("C.x", "C.x#2") and the
    worktree side by name ("C.x" twice, collapsed), so a byte-identical file
    with a property setter was stale forever."""

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        co._migrate(conn)
        return conn

    def test_identical_file_with_a_setter_is_not_stale(self):
        main = _write(self.root / "main", "m.py", SETTER)
        wt = _write(self.root / "wt", "m.py", SETTER)
        conn = self._conn()
        dep_graph.index_file(main, str(self.root / "main"), conn)
        with mock.patch.object(co, "_worktree_to_canonical", lambda f, c: main if f == wt else None):
            self.assertEqual(co._subgraph_stale(wt, set(), str(self.root / "wt"), conn), ([], []))

    def test_a_real_change_is_still_stale_and_named_by_label(self):
        main = _write(self.root / "main", "m.py", SETTER)
        wt = _write(self.root / "wt", "m.py", SETTER.replace("pass", "self._v = v"))
        conn = self._conn()
        dep_graph.index_file(main, str(self.root / "main"), conn)
        with mock.patch.object(co, "_worktree_to_canonical", lambda f, c: main if f == wt else None):
            nodes, files = co._subgraph_stale(wt, set(), str(self.root / "wt"), conn)
        self.assertEqual(nodes, ["m.py::C.x#2"])
        self.assertEqual(files, [wt])


class TestGraphFormatMigration(unittest.TestCase):
    """Item 9: a DB indexed with line-number node ids is dropped on open."""

    def test_old_graph_is_dropped_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "c.db")
            conn = sqlite3.connect(path)
            conn.executescript(co.SCHEMA)
            conn.execute("INSERT INTO nodes (node_id, file_path, name, kind, line_start, line_end, content_hash)"
                         " VALUES ('/x.py::12', '/x.py', NULL, 'benign', 12, 12, 'h')")
            conn.execute("INSERT INTO edges (from_file, to_file) VALUES ('/x.py', '/y.py')")
            conn.commit()
            co._migrate(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], co.GRAPH_FORMAT)
            conn.execute("INSERT INTO edges (from_file, to_file) VALUES ('/a.py', '/b.py')")
            conn.commit()
            co._migrate(conn)  # already current: keeps the new graph
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
