"""
tests/test_impact_review.py — lomitus.impact regressions found by an
independent review, each reproduced with a probe repo first.
"""
from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path

from lomitus import dep_graph, impact
from tests.test_impact import _GIT_ENV, _Repo


def _full_fixpoint(root: Path) -> sqlite3.Connection:
    """Reference: re-index every file until no kind changes."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(dep_graph.SCHEMA)
    files = impact._python_files(root, impact.DEFAULT_EXCLUDE)
    prev = None
    for _ in range(20):
        for f in files:
            dep_graph.index_file(f, str(root), conn)
        kinds = dict(conn.execute("SELECT node_id, kind FROM nodes"))
        if kinds == prev:
            break
        prev = kinds
    return conn


class TestDeletedAndRenamedFiles(_Repo):
    """Item 3: index_project sees only the head tree, so importers of a
    deleted module or of a renamed module's old path were never selected."""

    def test_importer_of_a_deleted_module_is_affected(self):
        self.write("src/app/gone.py", "def g():\n    return 1\n")
        self.write("tests/test_gone.py", "from app.gone import g\n\ndef test_g():\n    assert g() == 1\n")
        base = self.commit("add gone")
        (self.root / "src/app/gone.py").unlink()
        head = self.commit("delete gone")
        affected = impact.affected_files(self.root, base, head)
        self.assertIn("tests/test_gone.py", affected)
        self.assertNotIn("src/app/gone.py", affected)
        files = {(c.path, c.label, c.change) for c in impact.changed_nodes(self.root, base, head)}
        self.assertIn(("src/app/gone.py", "<file>", "deleted"), files)

    def test_importer_of_a_renamed_modules_old_path_is_affected(self):
        self.write("src/app/old.py", "def x():\n    return 1\n")
        self.write("tests/test_mv.py", "from app.old import x\n\ndef test_x():\n    assert x() == 1\n")
        base = self.commit("add old")
        self.git("mv", "src/app/old.py", "src/app/new.py")
        head = self.commit("pure rename")
        nodes = impact.changed_nodes(self.root, base, head)
        self.assertEqual([(c.path, c.label, c.change) for c in nodes], [("src/app/new.py", "<file>", "renamed")])
        self.assertIn("tests/test_mv.py", impact.affected_files(self.root, base, head))


class TestReordering(_Repo):
    """Item 4: file_digest sorted by label, so swapping top-level statements
    (import-time order) changed nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.write("src/app/boot.py", "from app import leaf\nleaf.leaf()\nimport app.core\n")
        self.write("tests/test_boot.py", "import app.boot\n\ndef test_boot():\n    pass\n")
        self.base2 = self.commit("boot")

    def test_swap_changes_closure_hash_and_is_reported(self):
        conn = impact.index_project(self.root)
        test = self.abs("tests/test_boot.py")
        before = impact.closure_hash(conn, test, self.root)
        (self.root / "src/app/boot.py").write_text("from app import leaf\nimport app.core\nleaf.leaf()\n")
        self.assertNotEqual(impact.closure_hash(conn, test, self.root), before)
        head = self.commit("swap")
        got = [(c.path, c.label, c.change) for c in impact.changed_nodes(self.root, self.base2, head)]
        self.assertEqual(got, [("src/app/boot.py", "<order>", "reordered")])


class TestIncrementalEqualsFullPasses(_Repo):
    """Item 5: effect typing followed qualified calls to files that were not
    edges, so the incremental passes could stop short of the full fixpoint."""

    def test_qualified_only_chain(self):
        # q1 is indexed before q2 and reaches it only through "app.q2.mid()".
        self.write("src/app/q1.py", "import app\n\ndef top():\n    return app.q2.mid()\n")
        self.write("src/app/q2.py", "from app import net\n\ndef mid():\n    return net.fetch('x')\n")
        inc = impact.index_project(self.root)
        ref = _full_fixpoint(self.root)
        q = "SELECT node_id, kind, content_hash FROM nodes"
        self.assertEqual(set(inc.execute(q)), set(ref.execute(q)))
        self.assertEqual(set(inc.execute("SELECT * FROM edges")), set(ref.execute("SELECT * FROM edges")))
        self.assertEqual(impact.node_effects(inc)[(self.abs("src/app/q1.py"), "top")], "external")

    def test_cap_is_reported_and_default_converges(self):
        for i in range(4):  # a chain c0 -> c1 -> c2 -> c3 -> net, indexed c0 first
            nxt = f"app.c{i + 1}.f()" if i < 3 else "app.net.fetch('x')"
            self.write(f"src/app/c{i}.py", f"import app\n\ndef f():\n    return {nxt}\n")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            capped = impact.index_project(self.root, effect_passes=2)
        self.assertTrue(any(issubclass(x.category, RuntimeWarning) for x in w))
        self.assertEqual(impact.index_stats(capped)["effects_converged"], "false")
        full = impact.index_project(self.root)
        self.assertEqual(impact.index_stats(full)["effects_converged"], "true")
        self.assertEqual(impact.node_effects(full)[(self.abs("src/app/c0.py"), "f")], "external")


class TestProjectInASubdirectory(unittest.TestCase):
    """Item 6: changed_files was repo-root relative and to_absolute joined
    onto the project root, so a project in a subdirectory selected nothing."""

    def test_selection_works_from_a_subdirectory_project(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d).resolve()
            proj = repo / "svc"

            def git(*a: str) -> str:
                return subprocess.run(["git", *a], cwd=repo, env=_GIT_ENV, check=True,
                                      capture_output=True, text=True, timeout=60).stdout.strip()

            def write(rel: str, body: str) -> None:
                p = proj / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body)

            git("init", "-q", "-b", "main")
            write("src/app/__init__.py", "")
            write("src/app/m.py", "def f():\n    return 1\n")
            write("tests/test_m.py", "from app.m import f\n")
            (repo / "README").write_text("x\n")
            git("add", "-A"); git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD")
            write("src/app/m.py", "def f():\n    return 2\n")
            (repo / "README").write_text("y\n")
            git("add", "-A"); git("commit", "-q", "-m", "change")
            head = git("rev-parse", "HEAD")
            dep_graph._roots_for.cache_clear()
            self.assertEqual([c.path for c in impact.changed_files(proj, base, head)], ["src/app/m.py"])
            self.assertEqual(impact.affected_files(proj, base, head), {"src/app/m.py", "tests/test_m.py"})
            with self.assertRaises(ValueError):
                impact.to_absolute(["svc/src/app/m.py"], proj)  # repo-relative by mistake


class TestClosureHashCoverage(_Repo):
    """Item 7: conftest.py and pytest_plugins modules are part of a test's
    result but were not in its closure; ast.dump differs across Pythons."""

    def test_conftest_and_plugins_are_covered(self):
        self.write("tests/conftest.py", 'pytest_plugins = ["tests.fixtures_db"]\n')
        self.write("tests/fixtures_db.py", "def make():\n    return 1\n")
        self.write("conftest.py", "ROOT = 1\n")
        conn = impact.index_project(self.root)
        test = self.abs("tests/test_core.py")
        h = impact.closure_hash(conn, test, self.root)
        for rel, new in (("tests/conftest.py", 'pytest_plugins = ["tests.fixtures_db"]\nX = 2\n'),
                         ("tests/fixtures_db.py", "def make():\n    return 2\n"),
                         ("conftest.py", "ROOT = 2\n")):
            (self.root / rel).write_text(new)
            h2 = impact.closure_hash(conn, test, self.root)
            self.assertNotEqual(h2, h, rel)
            h = h2

    def test_python_version_is_part_of_the_key(self):
        conn = impact.index_project(self.root)
        test = self.abs("tests/test_core.py")
        self.assertNotEqual(impact.closure_hash(conn, test, self.root),
                            impact.closure_hash(conn, test, self.root, include_python=False))


class TestExcludes(_Repo):
    def test_build_and_dist_only_at_the_top_level(self):
        self.write("src/app/build/steps.py", "def s():\n    return 1\n")
        self.write("build/lib/app/core.py", "X = 1\n")
        files = set(impact.to_relative(impact._python_files(self.root, impact.DEFAULT_EXCLUDE), self.root))
        self.assertIn("src/app/build/steps.py", files)
        self.assertNotIn("build/lib/app/core.py", files)


if __name__ == "__main__":
    unittest.main()
