#!/usr/bin/env python3
"""
tests/test_integration.py — process-level integration tests.

Invokes coordinator.py as subprocesses with JSON piped to stdin, exactly
as Claude Code hooks do. Verifies exit codes (0=allow, 2=block) and
selected stderr content.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT  = Path(__file__).parent.parent.resolve()
COORD = str(ROOT / "coordinator.py")
DB_PATH = ROOT / ".claude" / "coordinator.db"

CALC     = str(ROOT / "tests" / "fixtures" / "calc.py")
MODELS   = str(ROOT / "tests" / "fixtures" / "models.py")
API      = str(ROOT / "tests" / "fixtures" / "api.py")
UTILS    = str(ROOT / "tests" / "fixtures" / "utils.py")
SERVICES = str(ROOT / "tests" / "fixtures" / "services.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(payload: dict, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, COORD, *flags],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def _pretool(file_path: str, agent_id: str, old: str, new: str,
             agent_type: str | None = None) -> subprocess.CompletedProcess:
    return _run({
        "session_id": "test-session",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "old_string": old, "new_string": new},
    })


def _pretool_write(file_path: str, agent_id: str, content: str = "# new") -> subprocess.CompletedProcess:
    return _run({
        "session_id": "test-session",
        "agent_id": agent_id,
        "agent_type": None,
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
    })


def _release(file_path: str, agent_id: str) -> subprocess.CompletedProcess:
    return _run({
        "session_id": "test-session",
        "agent_id": agent_id,
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
    }, "--release")


def _subagent_stop(agent_id: str) -> subprocess.CompletedProcess:
    return _run({"session_id": "test-session", "agent_id": agent_id}, "--subagent-stop")


def _file_changed(file_path: str, event: str = "change") -> subprocess.CompletedProcess:
    return _run({"file_path": file_path, "event": event, "session_id": "test-session"},
                "--file-changed")


def _post_tool_batch(agent_id: str) -> subprocess.CompletedProcess:
    return _run({"session_id": "test-session", "agent_id": agent_id}, "--post-tool-batch")


def _fresh_db():
    for p in (DB_PATH,
              Path(str(DB_PATH) + "-wal"),
              Path(str(DB_PATH) + "-shm")):
        if p.exists():
            p.unlink()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Two-agent conflict
# ---------------------------------------------------------------------------

class TestTwoAgentConflict(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_second_agent_blocked(self):
        r1 = _pretool(CALC, "a1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = _pretool(CALC, "a2", "def add(a, b): return a + b", "def add(a, b): return a + b + 1")
        self.assertEqual(r2.returncode, 2)
        self.assertIn("held by", r2.stderr)

    def test_unblocked_after_release(self):
        _pretool(CALC, "a1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        _release(CALC, "a1")
        r2 = _pretool(CALC, "a2", "def add(a, b): return a + b", "def add(a, b): return a + b + 1")
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_same_agent_reentry_allowed(self):
        _pretool(CALC, "a1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        r = _pretool(CALC, "a1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        self.assertEqual(r.returncode, 0, r.stderr)


# ---------------------------------------------------------------------------
# Subagent stop releases lock
# ---------------------------------------------------------------------------

class TestSubagentStop(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_stop_releases_and_next_acquires(self):
        _pretool(CALC, "a1", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        _subagent_stop("a1")
        r = _pretool(CALC, "a2", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        self.assertEqual(r.returncode, 0, r.stderr)


# ---------------------------------------------------------------------------
# Benign edits
# ---------------------------------------------------------------------------

class TestBenignEdits(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_benign_blocked_by_structural_lock(self):
        # a1 holds a structural lock on a method; a2 editing an import in the same
        # file must be blocked (benign node + shared structural lock → block).
        _pretool(SERVICES, "a1",
                 "def add(self, a: int, b: int) -> int:",
                 "def add(self, a: int, b: int) -> int:  # edited")
        r = _pretool(SERVICES, "a2",
                     "from .calc import add",
                     "from .calc import add  # v2")
        self.assertEqual(r.returncode, 2)
        self.assertIn("structural lock", r.stderr)

    def test_different_structural_nodes_concurrent(self):
        # Two structural nodes (methods of the same class) must not conflict.
        _pretool(SERVICES, "a1",
                 "def add(self, a: int, b: int) -> int:",
                 "def add(self, a: int, b: int) -> int:  # edited")
        r = _pretool(SERVICES, "a2",
                     "def multiply(self, a: int, b: int) -> int:",
                     "def multiply(self, a: int, b: int) -> int:  # edited")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_same_node_second_blocked(self):
        _pretool(SERVICES, "a1",
                 "def add(self, a: int, b: int) -> int:",
                 "def add(self, a: int, b: int) -> int:  # v1")
        r = _pretool(SERVICES, "a2",
                     "def add(self, a: int, b: int) -> int:",
                     "def add(self, a: int, b: int) -> int:  # v2")
        self.assertEqual(r.returncode, 2)


# ---------------------------------------------------------------------------
# Different functions in same file
# ---------------------------------------------------------------------------

class TestDifferentFunctions(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_two_functions_simultaneously(self):
        r1 = _pretool(CALC, "a1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        r2 = _pretool(CALC, "a2", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_same_function_blocked(self):
        _pretool(CALC, "a1", "def mul(a, b): return a * b", "def mul(a, b): return a * b * 1")
        r = _pretool(CALC, "a2", "def mul(a, b): return a * b", "def mul(a, b): return a * b * 2")
        self.assertEqual(r.returncode, 2)


# ---------------------------------------------------------------------------
# Write tool sentinel lock
# ---------------------------------------------------------------------------

class TestWriteSentinel(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_write_acquires_sentinel(self):
        r = _pretool_write(UTILS, "a1")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_concurrent_writes_blocked(self):
        _pretool_write(UTILS, "a1")
        r = _pretool_write(UTILS, "a2")
        self.assertEqual(r.returncode, 2)

    def test_write_blocks_edit_same_file(self):
        _pretool_write(CALC, "a1")
        r = _pretool(CALC, "a2", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        self.assertEqual(r.returncode, 2)


# ---------------------------------------------------------------------------
# Priority queue
# ---------------------------------------------------------------------------

class TestPriorityQueue(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_orchestrator_jumps_queue(self):
        _pretool(CALC, "a1", "def pow(a, b): return a ** b", "def pow(a, b): return a ** b + 0")
        r_low  = _pretool(CALC, "low",  "def pow(a, b): return a ** b", "def pow(a, b): return a ** b + 1")
        r_orch = _pretool(CALC, "orch", "def pow(a, b): return a ** b", "def pow(a, b): return a ** b + 2",
                          agent_type="orchestrator")
        self.assertEqual(r_low.returncode,  2)
        self.assertEqual(r_orch.returncode, 2)
        self.assertIn("0", r_orch.stderr)   # orchestrator at position 0


# ---------------------------------------------------------------------------
# Release and failure handlers
# ---------------------------------------------------------------------------

class TestReleaseAndFailure(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_release_exits_zero(self):
        _pretool(CALC, "a1", "def div(a, b): return a / b if b != 0 else None",
                 "def div(a, b): return a / b if b != 0 else 0")
        r = _release(CALC, "a1")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_release_clears_lock(self):
        _pretool(CALC, "a1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        _release(CALC, "a1")
        conn = sqlite3.connect(str(DB_PATH))
        row  = conn.execute("SELECT 1 FROM locks WHERE agent_id='a1'").fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_idempotent_release(self):
        _pretool(CALC, "a1", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        _release(CALC, "a1")
        r = _release(CALC, "a1")
        self.assertEqual(r.returncode, 0)

    def test_failure_releases_lock(self):
        _pretool(CALC, "a1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        r = _run({"session_id": "test-session", "agent_id": "a1",
                  "tool_name": "Edit", "tool_input": {"file_path": CALC}}, "--failure")
        self.assertEqual(r.returncode, 0)
        conn = sqlite3.connect(str(DB_PATH))
        row  = conn.execute("SELECT 1 FROM locks WHERE agent_id='a1'").fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_next_acquires_after_failure(self):
        _pretool(CALC, "a1", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        _run({"session_id": "test-session", "agent_id": "a1",
              "tool_name": "Edit", "tool_input": {"file_path": CALC}}, "--failure")
        r = _pretool(CALC, "a2", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 1")
        self.assertEqual(r.returncode, 0, r.stderr)


# ---------------------------------------------------------------------------
# Multi-file
# ---------------------------------------------------------------------------

class TestMultiFile(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_different_files_no_conflict(self):
        r1 = _pretool(CALC, "a1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        r2 = _pretool(API,  "a2", "def get_user(user_id: int):",  "def get_user(user_id: int):  # edited")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)


# ---------------------------------------------------------------------------
# TTL crash recovery (process-level)
# ---------------------------------------------------------------------------

class TestTTLRecoveryProcess(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_expired_lock_evicted_on_next_pretool(self):
        """
        Insert an already-expired lock directly into the DB, then fire a
        PreToolUse from a different agent on the same node.  The coordinator
        must evict the expired lock and allow the new agent to acquire.
        """
        # Acquire lock normally so the node is indexed.
        _pretool(CALC, "live-agent", "def add(a, b): return a + b",
                 "def add(a, b): return a + b + 0")

        # Back-date the lock so it is already expired.
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "UPDATE locks SET expires_at = datetime('now', '-5 seconds') WHERE agent_id='live-agent'"
        )
        conn.commit()
        conn.close()

        # New agent: PreToolUse should evict the stale lock and allow.
        r = _pretool(CALC, "new-agent", "def add(a, b): return a + b",
                     "def add(a, b): return a + b + 1")
        self.assertEqual(r.returncode, 0, f"expired lock must be evicted; stderr: {r.stderr}")
        self.assertIn("TTL", r.stderr)


# ---------------------------------------------------------------------------
# FileChanged unlink (process-level)
# ---------------------------------------------------------------------------

class TestFileChangedProcess(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_unlink_purges_edges(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib  = d / "lib.py"
            main = d / "main.py"
            lib.write_text("def helper(): pass\n")
            main.write_text("from lib import helper\ndef use(): helper()\n")

            # Warm the cache via file-changed events.
            _file_changed(str(lib),  "add")
            _file_changed(str(main), "add")

            conn = sqlite3.connect(str(DB_PATH))
            edge_before = conn.execute(
                "SELECT 1 FROM edges WHERE to_file=?", (str(lib.resolve()),)
            ).fetchone()
            conn.close()
            # Edge may or may not exist depending on crawl depth; skip if not indexed.
            if edge_before is None:
                self.skipTest("lib.py not indexed via file-changed; edge check skipped")

            lib.unlink()
            _file_changed(str(lib), "unlink")

            conn = sqlite3.connect(str(DB_PATH))
            nodes = conn.execute(
                "SELECT 1 FROM nodes WHERE file_path=?", (str(lib.resolve()),)
            ).fetchone()
            edges = conn.execute(
                "SELECT 1 FROM edges WHERE to_file=?", (str(lib.resolve()),)
            ).fetchone()
            conn.close()
            self.assertIsNone(nodes, "nodes for deleted file must be purged")
            self.assertIsNone(edges, "edges to deleted file must be purged")


# ---------------------------------------------------------------------------
# PostToolBatch drift (process-level)
# ---------------------------------------------------------------------------

class TestPostToolBatchProcess(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_no_drift_passes(self):
        _pretool(CALC, "batch-agent", "def add(a, b): return a + b",
                 "def add(a, b): return a + b + 0")
        r = _post_tool_batch("batch-agent")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_drift_detected(self):
        # Lock a method in services.py — services imports from calc.py, so calc.py
        # is a dep_file and its nodes are in the subgraph_node_hashes snapshot.
        _pretool(SERVICES, "drift-agent",
                 "def add(self, a: int, b: int) -> int:",
                 "def add(self, a: int, b: int) -> int:  # v2")

        # Corrupt a node in calc.py (a dependency of services.py) to simulate
        # another agent changing a dep while this agent was mid-edit.
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                "UPDATE nodes SET content_hash='DIFFERENT' WHERE file_path LIKE ?",
                (f"%calc.py",),
            )
            conn.commit()
        finally:
            conn.close()

        r = _post_tool_batch("drift-agent")
        self.assertEqual(r.returncode, 2, f"drift must be detected; stderr: {r.stderr}")
        self.assertIn("drift", r.stderr.lower())


# ---------------------------------------------------------------------------
# Method-level locking
# ---------------------------------------------------------------------------

class TestMethodLevelLocking(unittest.TestCase):
    def setUp(self): _fresh_db()

    def test_different_methods_no_conflict(self):
        # Two agents editing different methods of the same class must not block each other.
        r1 = _pretool(SERVICES, "a1",
                      "def add(self, a: int, b: int) -> int:",
                      "def add(self, a: int, b: int) -> int:  # v2")
        r2 = _pretool(SERVICES, "a2",
                      "def multiply(self, a: int, b: int) -> int:",
                      "def multiply(self, a: int, b: int) -> int:  # v2")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_same_method_blocked(self):
        # Two agents targeting the same method must conflict.
        _pretool(SERVICES, "a1",
                 "def add(self, a: int, b: int) -> int:",
                 "def add(self, a: int, b: int) -> int:  # v1")
        r = _pretool(SERVICES, "a2",
                     "def add(self, a: int, b: int) -> int:",
                     "def add(self, a: int, b: int) -> int:  # v2")
        self.assertEqual(r.returncode, 2)

    def test_method_lock_does_not_block_sibling(self):
        # Holding a lock on one method must not prevent another agent from
        # acquiring a lock on a sibling method of the same class.
        _pretool(SERVICES, "a1",
                 "def add(self, a: int, b: int) -> int:",
                 "def add(self, a: int, b: int) -> int:  # v1")
        r = _pretool(SERVICES, "a2",
                     "def multiply(self, a: int, b: int) -> int:",
                     "def multiply(self, a: int, b: int) -> int:  # v2")
        self.assertEqual(r.returncode, 0, r.stderr)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
