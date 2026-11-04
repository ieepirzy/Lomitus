#!/usr/bin/env python3
"""
tests/test_graph_correctness.py — dependency-graph correctness on project
shapes the original resolver got wrong: function-local imports, src layouts,
"from pkg import submodule", qualified calls, string imports, stable node ids,
duplicate definitions (property setters), class fields and decorators.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from lomitus import dep_graph
from lomitus.dep_graph import (
    SCHEMA,
    identify_target_nodes,
    import_roots,
    index_file,
    parse_file,
)


def _write(root: Path, rel: str, body: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return str(p.resolve())


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


def _edges(conn: sqlite3.Connection, from_file: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT to_file FROM edges WHERE from_file = ?", (from_file,))}


def _kind(conn: sqlite3.Connection, file_path: str, name: str) -> str | None:
    row = conn.execute("SELECT kind FROM nodes WHERE file_path = ? AND name = ?", (file_path, name)).fetchone()
    return row[0] if row else None


class _Project(unittest.TestCase):
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


class TestImportsAtAnyDepth(_Project):
    def test_function_local_import_is_an_edge(self):
        """The shape of the escape: a module imported only inside a function."""
        _write(self.root, "app/__init__.py", "")
        tel = _write(self.root, "app/telemetry.py", "def emit(x):\n    return x\n")
        db = _write(self.root, "app/database.py", """
            def connect():
                from app.telemetry import emit
                return emit(1)
        """)
        conn = _conn()
        index_file(db, str(self.root), conn)
        self.assertIn(tel, _edges(conn, db))

    def test_type_checking_and_try_imports_are_edges(self):
        _write(self.root, "app/__init__.py", "")
        a = _write(self.root, "app/a.py", "X = 1\n")
        b = _write(self.root, "app/b.py", "Y = 2\n")
        c = _write(self.root, "app/c.py", """
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from app.a import X
            try:
                from app.b import Y
            except ImportError:
                Y = None
        """)
        conn = _conn()
        index_file(c, str(self.root), conn)
        self.assertTrue({a, b} <= _edges(conn, c), _edges(conn, c))

    def test_string_literal_import_module_is_an_edge(self):
        _write(self.root, "app/__init__.py", "")
        plug = _write(self.root, "app/plugins/csv_source.py", "def load():\n    return []\n")
        _write(self.root, "app/plugins/__init__.py", "")
        reg = _write(self.root, "app/registry.py", """
            import importlib
            def load():
                return importlib.import_module("app.plugins.csv_source")
        """)
        conn = _conn()
        index_file(reg, str(self.root), conn)
        self.assertIn(plug, _edges(conn, reg))


class TestSrcLayout(_Project):
    def test_src_package_and_top_level_tests_both_resolve(self):
        _write(self.root, "pyproject.toml", '[project]\nname = "app"\n')
        _write(self.root, "src/app/__init__.py", "")
        core = _write(self.root, "src/app/core.py", "def run():\n    return 1\n")
        _write(self.root, "tests/__init__.py", "")
        helpers = _write(self.root, "tests/helpers.py", "def h():\n    return 2\n")
        test = _write(self.root, "tests/test_core.py", """
            from app.core import run
            from tests.helpers import h
            def test_run():
                assert run() + h() == 3
        """)
        conn = _conn()
        index_file(test, str(self.root), conn)
        self.assertTrue({core, helpers} <= _edges(conn, test), _edges(conn, test))

    def test_import_roots_from_env_and_pyproject(self):
        (self.root / "lib").mkdir()
        (self.root / "other").mkdir()
        _write(self.root, "pyproject.toml", '[tool.lomitus]\nimport_roots = ["lib"]\n')
        self.assertEqual(import_roots(str(self.root)), (self.root / "lib",))
        dep_graph._roots_for.cache_clear()
        with mock.patch.dict(os.environ, {"LOMITUS_IMPORT_ROOTS": os.pathsep.join(["other", "."])}):
            self.assertEqual(import_roots(str(self.root)), (self.root / "other", self.root))

    def test_hatch_wheel_packages_give_the_import_root(self):
        _write(self.root, "pyproject.toml", '[tool.hatch.build.targets.wheel]\npackages = ["code/app"]\n')
        _write(self.root, "code/app/__init__.py", "")
        self.assertEqual(import_roots(str(self.root))[0], self.root / "code")


class TestFromPackageImportSubmodule(_Project):
    def test_from_pkg_import_mod_is_an_edge_to_the_submodule(self):
        _write(self.root, "app/__init__.py", "")
        mod = _write(self.root, "app/mod.py", "def f():\n    return 1\n")
        use = _write(self.root, "app/use.py", "from app import mod\n\ndef g():\n    return mod.f()\n")
        rel = _write(self.root, "app/rel.py", "from . import mod\n")
        conn = _conn()
        index_file(use, str(self.root), conn)
        index_file(rel, str(self.root), conn)
        self.assertIn(mod, _edges(conn, use))
        self.assertIn(mod, _edges(conn, rel))


class TestQualifiedCalls(_Project):
    def test_qualified_calls_carry_external_effects_across_files(self):
        _write(self.root, "app/__init__.py", "")
        net = _write(self.root, "app/net.py", """
            import requests
            def fetch(url):
                return requests.get(url)
            class Client:
                def get(self, url):
                    return requests.get(url)
        """)
        svc = _write(self.root, "app/svc.py", """
            import app.net
            import app.net as n
            from app import net

            def via_alias():
                return n.fetch("x")

            def via_submodule_binding():
                return net.fetch("x")

            def via_full_path():
                return app.net.fetch("x")

            def via_class_method():
                return net.Client.get(None, "x")

            def pure():
                return 1
        """)
        conn = _conn()
        index_file(net, str(self.root), conn)
        index_file(svc, str(self.root), conn)
        for name in ("via_alias", "via_submodule_binding", "via_full_path", "via_class_method"):
            self.assertEqual(_kind(conn, svc, name), "external", name)
        self.assertEqual(_kind(conn, svc, "pure"), "structural")

    def test_function_local_from_import_carries_external_effects(self):
        _write(self.root, "app/__init__.py", "")
        net = _write(self.root, "app/net.py", "import requests\ndef fetch(u):\n    return requests.get(u)\n")
        svc = _write(self.root, "app/svc.py", "def go():\n    from app.net import fetch\n    return fetch('x')\n")
        conn = _conn()
        index_file(net, str(self.root), conn)
        index_file(svc, str(self.root), conn)
        self.assertEqual(_kind(conn, svc, "go"), "external")


class TestNodeIdentity(_Project):
    def _ids(self, path: str) -> set[str]:
        return {n.node_id.split("::", 1)[1] for n in parse_file(path)}

    def test_ids_are_stable_when_code_moves(self):
        f = _write(self.root, "m.py", """
            import os
            LIMIT = 3
            if os.environ.get("X"):
                FLAG = True
            def f():
                return LIMIT
        """)
        before = self._ids(f)
        Path(f).write_text("# header\n\n\n" + Path(f).read_text(), encoding="utf-8")
        self.assertEqual(self._ids(f), before)
        self.assertIn("var:LIMIT", before)
        self.assertIn("import:os", before)
        self.assertFalse(any(label.isdigit() for label in before), before)

    def test_property_setter_and_overloads_index_without_collision(self):
        f = _write(self.root, "p.py", """
            class C:
                @property
                def x(self):
                    return 1
                @x.setter
                def x(self, v):
                    pass
        """)
        conn = _conn()
        index_file(f, str(self.root), conn)  # raised IntegrityError before
        ids = {r[0].split("::", 1)[1] for r in conn.execute("SELECT node_id FROM nodes")}
        self.assertEqual(ids, {"C", "C.x", "C.x#2"})


class TestClassFieldsAndDecorators(_Project):
    def _hashes(self, path: str) -> dict[str, str]:
        return {n.node_id.split("::", 1)[1]: n.content_hash for n in parse_file(path)}

    def test_class_field_change_changes_the_header_not_the_methods(self):
        f = _write(self.root, "c.py", """
            class Model:
                retries: int = 3
                def run(self):
                    return self.retries
        """)
        before = self._hashes(f)
        Path(f).write_text(Path(f).read_text().replace("= 3", "= 5"), encoding="utf-8")
        after = self._hashes(f)
        self.assertNotEqual(before["Model"], after["Model"])
        self.assertEqual(before["Model.run"], after["Model.run"])
        Path(f).write_text(Path(f).read_text().replace("return self.retries", "return 0"), encoding="utf-8")
        self.assertEqual(self._hashes(f)["Model"], after["Model"])

    def test_decorator_change_changes_the_function_hash_and_lines(self):
        f = _write(self.root, "d.py", """
            import functools
            @functools.lru_cache(maxsize=1)
            def f():
                return 1
        """)
        node = next(n for n in parse_file(f) if n.name == "f")
        self.assertEqual(node.line_start, 2)
        Path(f).write_text(Path(f).read_text().replace("maxsize=1", "maxsize=None"), encoding="utf-8")
        self.assertNotEqual(node.content_hash, next(n for n in parse_file(f) if n.name == "f").content_hash)

    def test_class_decorator_change_changes_the_header(self):
        f = _write(self.root, "e.py", """
            import dataclasses
            @dataclasses.dataclass(frozen=True)
            class P:
                x: int
        """)
        before = self._hashes(f)["P"]
        Path(f).write_text(Path(f).read_text().replace("frozen=True", "frozen=False"), encoding="utf-8")
        self.assertNotEqual(before, self._hashes(f)["P"])

    def test_header_node_targets_only_its_own_lines(self):
        f = _write(self.root, "t.py", """
            class K:
                limit = 1

                def a(self):
                    return 1

                def b(self):
                    return 2
        """)
        conn = _conn()
        index_file(f, str(self.root), conn)

        def labels(old: str) -> set[str]:
            got = identify_target_nodes(f, "Edit", {"old_string": old}, conn)
            return {g.split("::", 1)[1] for g in got}

        self.assertEqual(labels("limit = 1"), {"K"})
        self.assertEqual(labels("return 1"), {"K.a"})
        self.assertEqual(labels("        return 1\n\n    def b"), {"K.a", "K.b"})


if __name__ == "__main__":
    unittest.main()


class TestSegmentCache(unittest.TestCase):
    def test_cached_segments_equal_ast_get_source_segment(self):
        """_segment splits each source once; it must return exactly what
        ast.get_source_segment returns, or every stored hash would shift."""
        import ast
        src = (Path(dep_graph.__file__)).read_text(encoding="utf-8")
        src += "\nx = 'caf\\u00e9 \\u2603'  # non-ASCII columns\nclass Q:\n    a: int = 1\n"
        tree = ast.parse(src)
        checked = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.stmt, ast.Call, ast.Constant)):
                self.assertEqual(dep_graph._segment(src, node), ast.get_source_segment(src, node) or "")
                checked += 1
        self.assertGreater(checked, 500)
