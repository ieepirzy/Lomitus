"""
tests/test_impact.py — lomitus.impact, the CI-facing library API: changed
files and nodes between revisions, reverse/forward closures, node effects,
and closure hashes as result-cache keys.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from lomitus import dep_graph, impact

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1", "HOME": tempfile.gettempdir(),
}


class _Repo(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        dep_graph._roots_for.cache_clear()
        self.git("init", "-q", "-b", "main")
        self.write("pyproject.toml", '[project]\nname = "app"\n')
        self.write("src/app/__init__.py", "")
        self.write("src/app/net.py", """
            import requests

            def fetch(url):
                return requests.get(url)
        """)
        self.write("src/app/core.py", """
            from app import net

            LIMIT = 3

            def run(url):
                # pull the page
                return net.fetch(url)

            def pure(x):
                return x + LIMIT
        """)
        self.write("src/app/leaf.py", "def leaf():\n    return 1\n")
        self.write("tests/__init__.py", "")
        self.write("tests/test_core.py", """
            from app.core import run, pure

            def test_pure():
                assert pure(1) == 4
        """)
        self.write("tests/test_leaf.py", """
            def test_leaf():
                from app.leaf import leaf
                assert leaf() == 1
        """)
        self.base = self.commit("base")

    def tearDown(self) -> None:
        dep_graph._roots_for.cache_clear()
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, env=_GIT_ENV, check=True,
                              capture_output=True, text=True, timeout=60).stdout.strip()

    def write(self, rel: str, body: str) -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
        return str(p)

    def commit(self, msg: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def abs(self, rel: str) -> str:
        return str((self.root / rel).resolve())


class TestChanges(_Repo):
    def test_changed_files_with_rename_and_delete(self):
        self.git("mv", "src/app/leaf.py", "src/app/leaves.py")
        (self.root / "tests/test_leaf.py").unlink()
        self.write("src/app/new.py", "X = 1\n")
        head = self.commit("change")
        got = {(c.path, c.status, c.old_path) for c in impact.changed_files(self.root, self.base, head)}
        self.assertEqual(got, {
            ("src/app/leaves.py", "renamed", "src/app/leaf.py"),
            ("tests/test_leaf.py", "deleted", None),
            ("src/app/new.py", "added", None),
        })

    def test_comment_and_format_only_edits_change_no_node_in_ast_mode(self):
        core = self.root / "src/app/core.py"
        core.write_text(core.read_text().replace("# pull the page", "# fetch it").replace("x + LIMIT", "x+LIMIT"))
        head = self.commit("cosmetic")
        self.assertEqual(impact.changed_nodes(self.root, self.base, head), [])
        source_mode = {c.label for c in impact.changed_nodes(self.root, self.base, head, hash_mode="source")}
        self.assertEqual(source_mode, {"run", "pure"})

    def test_changed_nodes_names_the_function_and_module_statement(self):
        core = self.root / "src/app/core.py"
        core.write_text(core.read_text().replace("x + LIMIT", "x * LIMIT").replace("LIMIT = 3", "LIMIT = 4"))
        head = self.commit("logic")
        got = {(c.path, c.label, c.change, c.is_function) for c in impact.changed_nodes(self.root, self.base, head)}
        self.assertEqual(got, {
            ("src/app/core.py", "pure", "modified", True),
            ("src/app/core.py", "var:LIMIT", "modified", False),
        })

    def test_unparseable_file_is_reported_not_skipped(self):
        self.write("src/app/leaf.py", "def leaf(:\n")
        head = self.commit("broken")
        got = impact.changed_nodes(self.root, self.base, head)
        self.assertEqual([(c.path, c.change) for c in got], [("src/app/leaf.py", "unparseable")])


class TestGraph(_Repo):
    def test_reverse_closure_selects_tests_through_src_layout_and_local_imports(self):
        conn = impact.index_project(self.root)
        sel = impact.to_relative(impact.reverse_closure(conn, [self.abs("src/app/net.py")]), self.root)
        self.assertEqual(sel, {"src/app/net.py", "src/app/core.py", "tests/test_core.py"})
        sel = impact.to_relative(impact.reverse_closure(conn, [self.abs("src/app/leaf.py")]), self.root)
        self.assertEqual(sel, {"src/app/leaf.py", "tests/test_leaf.py"})

    def test_effects_cross_files_through_qualified_calls(self):
        conn = impact.index_project(self.root)
        eff = impact.node_effects(conn, self.abs("src/app/core.py"))
        self.assertEqual(eff[(self.abs("src/app/core.py"), "run")], "external")
        self.assertEqual(eff[(self.abs("src/app/core.py"), "pure")], "structural")

    def test_effects_settle_across_a_chain_regardless_of_order(self):
        # a -> b -> net: two hops, indexed in an order that needs a second pass.
        self.write("src/app/a_first.py", "from app import b_mid\ndef top():\n    return b_mid.mid()\n")
        self.write("src/app/b_mid.py", "from app import net\ndef mid():\n    return net.fetch('x')\n")
        conn = impact.index_project(self.root)
        self.assertEqual(impact.node_effects(conn)[(self.abs("src/app/a_first.py"), "top")], "external")

    def test_excluded_directories_are_not_indexed(self):
        self.write(".venv/lib/site.py", "import app.net\n")
        conn = impact.index_project(self.root)
        files = {r[0] for r in conn.execute("SELECT DISTINCT file_path FROM nodes")}
        self.assertFalse(any("/.venv/" in f for f in files))


class TestClosureHash(_Repo):
    def test_stable_across_checkouts_and_cosmetic_edits_and_sensitive_to_logic(self):
        conn = impact.index_project(self.root)
        test = self.abs("tests/test_core.py")
        h0 = impact.closure_hash(conn, test, self.root)
        # A second checkout at another path hashes the same.
        with tempfile.TemporaryDirectory() as other:
            dst = Path(other) / "clone"
            subprocess.run(["git", "clone", "-q", str(self.root), str(dst)], check=True, timeout=60, env=_GIT_ENV)
            dep_graph._roots_for.cache_clear()
            c2 = impact.index_project(dst)
            self.assertEqual(impact.closure_hash(c2, str(dst / "tests/test_core.py"), dst), h0)
        core = self.root / "src/app/core.py"
        core.write_text(core.read_text().replace("# pull the page", "# reformatted"))
        self.assertEqual(impact.closure_hash(conn, test, self.root), h0)
        net = self.root / "src/app/net.py"
        net.write_text(net.read_text().replace("requests.get(url)", "requests.post(url)"))
        self.assertNotEqual(impact.closure_hash(conn, test, self.root), h0)
        # leaf.py is outside test_core's closure: changing it keeps the hash.
        h1 = impact.closure_hash(conn, test, self.root)
        (self.root / "src/app/leaf.py").write_text("def leaf():\n    return 2\n")
        self.assertEqual(impact.closure_hash(conn, test, self.root), h1)
        self.assertNotEqual(impact.closure_hash(conn, test, self.root, extra=["py3.13"]), h1)


if __name__ == "__main__":
    unittest.main()
