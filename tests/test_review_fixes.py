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


def _edges(conn: sqlite3.Connection, from_file: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT to_file FROM edges WHERE from_file = ?", (from_file,))}


class TestAncestorPackageInits(_Tmp):
    """Item 2: importing pkg.sub.deep runs pkg/__init__.py and
    pkg/sub/__init__.py first, so a change there affects the importer."""

    def setUp(self) -> None:
        super().setUp()
        self.pkg_init = _write(self.root, "pkg/__init__.py", "CONFIG = {}\n")
        self.sub_init = _write(self.root, "pkg/sub/__init__.py", "")
        _write(self.root, "pkg/mod.py", "def f():\n    return 1\n")
        _write(self.root, "pkg/sub/deep.py", "def g():\n    return 2\n")
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(dep_graph.SCHEMA)

    def _index(self, rel: str, body: str) -> str:
        f = _write(self.root, rel, body)
        dep_graph.index_file(f, str(self.root), self.conn)
        return f

    def test_plain_import_of_a_submodule(self):
        a = self._index("a.py", "import pkg.mod\n")
        self.assertIn(self.pkg_init, _edges(self.conn, a))

    def test_from_deep_module_import_name(self):
        b = self._index("b.py", "from pkg.sub.deep import g\n")
        self.assertTrue({self.pkg_init, self.sub_init} <= _edges(self.conn, b), _edges(self.conn, b))

    def test_relative_import_of_a_deep_module(self):
        r = self._index("pkg/rel.py", "from .sub.deep import g\n")
        self.assertIn(self.sub_init, _edges(self.conn, r))

    def test_string_import(self):
        c = self._index("c.py", "import importlib\nimportlib.import_module('pkg.sub.deep')\n")
        self.assertTrue({self.pkg_init, self.sub_init} <= _edges(self.conn, c))

    def test_reverse_crawl_from_a_package_init_reaches_importers_of_its_submodules(self):
        a = self._index("a.py", "import pkg.mod\n")
        b = self._index("b.py", "from pkg.sub.deep import g\n")
        got = dep_graph.crawl_subgraph(self.sub_init, depth=5, conn=self.conn, direction="reverse")
        self.assertIn(b, got)
        self.assertNotIn(a, got)  # pkg.mod does not run pkg/sub/__init__.py


class TestStarImportAll(_Tmp):
    def test_star_import_edges_to_the_submodules_all_names(self):
        _write(self.root, "pkg/__init__.py", '__all__ = ["one", "helper"]\nhelper = 1\n')
        one = _write(self.root, "pkg/one.py", "X = 1\n")
        _write(self.root, "pkg/two.py", "Y = 2\n")
        conn = sqlite3.connect(":memory:")
        conn.executescript(dep_graph.SCHEMA)
        s = _write(self.root, "s.py", "from pkg import *\n")
        dep_graph.index_file(s, str(self.root), conn)
        got = _edges(conn, s)
        self.assertIn(one, got)
        self.assertNotIn(str((self.root / "pkg/two.py").resolve()), got)


if __name__ == "__main__":
    unittest.main()
