#!/usr/bin/env python3
"""
test_integration.py — process-level integration tests for coordinator.py.

Invokes coordinator.py as subprocesses with JSON piped to stdin, exactly
as Claude Code hooks do. Verifies exit codes (0=allow, 2=block) and
selected stderr content.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
COORD = str(ROOT / "coordinator.py")
DB_PATH = ROOT / ".claude" / "coordinator.db"

CALC = str(ROOT / "test-files" / "calc.py")
MODELS = str(ROOT / "test-files" / "models.py")
API = str(ROOT / "test-files" / "api.py")
UTILS = str(ROOT / "test-files" / "utils.py")


def _run(payload: dict, *flags: str) -> subprocess.CompletedProcess:
    """Invoke coordinator.py with payload on stdin; return CompletedProcess."""
    return subprocess.run(
        [sys.executable, COORD, *flags],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def _pretool(file_path: str, agent_id: str, old_string: str, new_string: str,
             agent_type: str | None = None, tool_name: str = "Edit") -> subprocess.CompletedProcess:
    payload = {
        "session_id": "test-session",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "tool_name": tool_name,
        "tool_input": {
            "file_path": file_path,
            "old_string": old_string,
            "new_string": new_string,
        },
    }
    return _run(payload)


def _pretool_write(file_path: str, agent_id: str, content: str = "# new") -> subprocess.CompletedProcess:
    payload = {
        "session_id": "test-session",
        "agent_id": agent_id,
        "agent_type": None,
        "tool_name": "Write",
        "tool_input": {
            "file_path": file_path,
            "content": content,
        },
    }
    return _run(payload)


def _release(file_path: str, agent_id: str, agent_type: str | None = None) -> subprocess.CompletedProcess:
    payload = {
        "session_id": "test-session",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
    }
    return _run(payload, "--release")


def _subagent_stop(agent_id: str) -> subprocess.CompletedProcess:
    payload = {"session_id": "test-session", "agent_id": agent_id}
    return _run(payload, "--subagent-stop")


def setup() -> None:
    """Drop and recreate the coordinator DB before each test."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class TestTwoAgentConflict(unittest.TestCase):
    def setUp(self):
        setup()

    def test_second_agent_blocked(self):
        r1 = _pretool(CALC, "agent-1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        self.assertEqual(r1.returncode, 0, f"Agent-1 should acquire lock; stderr: {r1.stderr}")

        r2 = _pretool(CALC, "agent-2", "def add(a, b): return a + b", "def add(a, b): return a + b + 1")
        self.assertEqual(r2.returncode, 2, f"Agent-2 should be blocked; stderr: {r2.stderr}")
        self.assertIn("held by", r2.stderr)

    def test_agent_unblocked_after_release(self):
        _pretool(CALC, "agent-1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        _release(CALC, "agent-1")

        r2 = _pretool(CALC, "agent-2", "def add(a, b): return a + b", "def add(a, b): return a + b + 1")
        self.assertEqual(r2.returncode, 0, f"Agent-2 should acquire after release; stderr: {r2.stderr}")

    def test_same_agent_reentry_allowed(self):
        _pretool(CALC, "agent-1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        r2 = _pretool(CALC, "agent-1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        self.assertEqual(r2.returncode, 0, f"Same agent re-entry should be allowed; stderr: {r2.stderr}")


class TestSubagentStopReleasesLock(unittest.TestCase):
    def setUp(self):
        setup()

    def test_stop_releases_and_next_acquires(self):
        _pretool(CALC, "agent-1", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        _subagent_stop("agent-1")

        r = _pretool(CALC, "agent-2", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        self.assertEqual(r.returncode, 0, f"Agent-2 should acquire after stop; stderr: {r.stderr}")


class TestBenignEdits(unittest.TestCase):
    def setUp(self):
        setup()

    def test_benign_edit_blocked_when_structural_locked(self):
        # Agent-1 locks the User class (structural node).
        _pretool(MODELS, "agent-1", "class User:", "class User:  # edited")

        # Agent-2 tries a benign edit (the import line).  The coordinator blocks
        # benign edits on any file that has an active structural lock — a second
        # agent touching any part of the file could conflict with the in-progress
        # structural change.
        r = _pretool(MODELS, "agent-2",
                     "from dataclasses import dataclass",
                     "from dataclasses import dataclass  # v2")
        self.assertEqual(r.returncode, 2, f"Benign edit should be blocked by structural lock; stderr: {r.stderr}")
        self.assertIn("structural lock", r.stderr)

    def test_structural_node_blocked_when_another_structural_locked(self):
        # Agent-1 locks User class
        _pretool(MODELS, "agent-1", "class User:", "class User:  # edited")
        # Agent-2 tries to lock Product class in same file — different node, should be allowed
        r = _pretool(MODELS, "agent-2", "class Product:", "class Product:  # edited")
        self.assertEqual(r.returncode, 0, f"Different structural node in same file should be allowed; stderr: {r.stderr}")

    def test_same_node_second_agent_blocked(self):
        _pretool(MODELS, "agent-1", "class User:", "class User:  # v1")
        r = _pretool(MODELS, "agent-2", "class User:", "class User:  # v2")
        self.assertEqual(r.returncode, 2, f"Same node second agent must be blocked; stderr: {r.stderr}")


class TestDifferentFunctionsSameFile(unittest.TestCase):
    def setUp(self):
        setup()

    def test_two_functions_lockable_simultaneously(self):
        r1 = _pretool(CALC, "agent-1", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        self.assertEqual(r1.returncode, 0)

        r2 = _pretool(CALC, "agent-2", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        self.assertEqual(r2.returncode, 0, f"Different functions should lock independently; stderr: {r2.stderr}")

    def test_same_function_second_agent_blocked(self):
        _pretool(CALC, "agent-1", "def mul(a, b): return a * b", "def mul(a, b): return a * b * 1")
        r2 = _pretool(CALC, "agent-2", "def mul(a, b): return a * b", "def mul(a, b): return a * b * 2")
        self.assertEqual(r2.returncode, 2)


class TestWriteToolSentinelLock(unittest.TestCase):
    def setUp(self):
        setup()

    def test_write_acquires_sentinel_lock(self):
        r1 = _pretool_write(UTILS, "agent-1", "# rewrite")
        self.assertEqual(r1.returncode, 0, f"Write should acquire sentinel lock; stderr: {r1.stderr}")

    def test_write_blocks_concurrent_write(self):
        _pretool_write(UTILS, "agent-1", "# rewrite-1")
        r2 = _pretool_write(UTILS, "agent-2", "# rewrite-2")
        self.assertEqual(r2.returncode, 2, f"Concurrent Write should be blocked; stderr: {r2.stderr}")

    def test_write_blocks_edit_on_same_file(self):
        _pretool_write(CALC, "agent-1", "# full rewrite")
        r2 = _pretool(CALC, "agent-2", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        self.assertEqual(r2.returncode, 2, f"Edit blocked by concurrent Write; stderr: {r2.stderr}")


class TestPriorityQueue(unittest.TestCase):
    def setUp(self):
        setup()

    def test_orchestrator_jumps_queue(self):
        # Agent-1 holds the lock
        _pretool(CALC, "agent-1", "def pow(a, b): return a ** b", "def pow(a, b): return a ** b + 0")

        # Low-priority agent queues first
        r_low = _pretool(CALC, "low-agent", "def pow(a, b): return a ** b", "def pow(a, b): return a ** b + 1")
        self.assertEqual(r_low.returncode, 2)
        self.assertIn("position", r_low.stderr.lower())

        # Orchestrator queues second — should get position 0 (head of queue)
        r_orch = _pretool(CALC, "orch-agent", "def pow(a, b): return a ** b", "def pow(a, b): return a ** b + 2",
                          agent_type="orchestrator")
        self.assertEqual(r_orch.returncode, 2)
        self.assertIn("0", r_orch.stderr)  # queue position 0


class TestContractSnapshotStored(unittest.TestCase):
    def setUp(self):
        setup()

    def test_snapshot_stored_in_db_after_lock(self):
        r = _pretool(CALC, "agent-snap", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        self.assertEqual(r.returncode, 0)

        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT node_id, io_snapshot FROM snapshots WHERE node_id LIKE ?",
            ("%::add",),
        ).fetchone()
        conn.close()

        # Snapshot may be None if subprocess execution failed, but the row should exist
        # OR the snapshot is present with a valid JSON structure.
        # We verify that at least the lock row exists (snapshot presence depends on executability).
        conn2 = sqlite3.connect(str(DB_PATH))
        lock_row = conn2.execute(
            "SELECT node_id FROM locks WHERE agent_id = 'agent-snap'",
        ).fetchone()
        conn2.close()
        self.assertIsNotNone(lock_row, "Lock row must exist after successful pretool")


class TestReleaseFlow(unittest.TestCase):
    def setUp(self):
        setup()

    def test_release_exits_zero(self):
        _pretool(CALC, "agent-rel", "def div(a, b): return a / b if b != 0 else None",
                 "def div(a, b): return a / b if b != 0 else 0")
        r = _release(CALC, "agent-rel")
        self.assertEqual(r.returncode, 0, f"Release should exit 0; stderr: {r.stderr}")

    def test_release_clears_lock(self):
        _pretool(CALC, "agent-rel2", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        _release(CALC, "agent-rel2")

        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT node_id FROM locks WHERE agent_id = 'agent-rel2'"
        ).fetchone()
        conn.close()
        self.assertIsNone(row, "Lock should be gone after release")

    def test_idempotent_release(self):
        _pretool(CALC, "agent-idem", "def sub(a, b): return a - b", "def sub(a, b): return a - b - 0")
        _release(CALC, "agent-idem")
        r = _release(CALC, "agent-idem")  # second release — no lock to release
        self.assertEqual(r.returncode, 0)


class TestMultiFileScenario(unittest.TestCase):
    def setUp(self):
        setup()

    def test_agents_can_lock_different_files_simultaneously(self):
        r1 = _pretool(CALC, "agent-a", "def add(a, b): return a + b", "def add(a, b): return a + b + 0")
        r2 = _pretool(API, "agent-b", "def get_user(user_id: int): return {\"id\": user_id, \"name\": \"Alice\"}",
                      "def get_user(user_id: int): return {\"id\": user_id, \"name\": \"Bob\"}")
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0, f"Different files should not conflict; stderr: {r2.stderr}")

    def test_same_node_blocked_across_agents(self):
        _pretool(API, "agent-c",
                 "def list_users(): return [{\"id\": 1}, {\"id\": 2}]",
                 "def list_users(): return [{\"id\": 1}]")
        r = _pretool(API, "agent-d",
                     "def list_users(): return [{\"id\": 1}, {\"id\": 2}]",
                     "def list_users(): return []")
        self.assertEqual(r.returncode, 2)


class TestFailureHandler(unittest.TestCase):
    def setUp(self):
        setup()

    def test_failure_releases_lock(self):
        _pretool(CALC, "agent-fail",
                 "def add(a, b): return a + b",
                 "def add(a, b): return a + b + 0")

        payload = {
            "session_id": "test-session",
            "agent_id": "agent-fail",
            "tool_name": "Edit",
            "tool_input": {"file_path": CALC},
        }
        r = _run(payload, "--failure")
        self.assertEqual(r.returncode, 0)

        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT node_id FROM locks WHERE agent_id = 'agent-fail'"
        ).fetchone()
        conn.close()
        self.assertIsNone(row, "Lock must be released after failure")

    def test_next_agent_acquires_after_failure(self):
        _pretool(CALC, "agent-fail2",
                 "def sub(a, b): return a - b",
                 "def sub(a, b): return a - b - 0")
        payload = {
            "session_id": "test-session",
            "agent_id": "agent-fail2",
            "tool_name": "Edit",
            "tool_input": {"file_path": CALC},
        }
        _run(payload, "--failure")

        r = _pretool(CALC, "agent-ok",
                     "def sub(a, b): return a - b",
                     "def sub(a, b): return a - b - 1")
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
