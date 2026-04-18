#!/usr/bin/env python3
"""
Test suite for the keep-working CLI (bin/keep-working).

Tests run against a sandboxed CLAUDE_HOME so they don't touch your real
~/.claude/.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "keep-working"
assert CLI.exists(), f"CLI not found at {CLI}"


class CLITestBase(unittest.TestCase):

    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="kw-cli-"))
        self.env = os.environ.copy()
        # CLI reads CLAUDE_HOME env var, falls back to ~/.claude
        self.env["CLAUDE_HOME"] = str(self.sandbox / ".claude")
        self.claude = self.sandbox / ".claude"
        self.sessions = self.claude / "keep-working"
        self.sessions.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def run_cli(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(CLI), *args],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def make_session(self, sid: str, *, deadline_offset: int = 3600,
                     task: str = "test", nudge: int = 0,
                     max_turns: int = 200, max_tokens: int = 5_000_000):
        import hashlib, re
        h = hashlib.sha1(sid.encode()).hexdigest()[:16]
        prefix = re.sub(r"[^A-Za-z0-9_\-]", "_", sid)[:32]
        sf = self.sessions / f"{prefix}_{h}.json"
        sf.write_text(json.dumps({
            "active": True,
            "session_id": sid,
            "deadline_epoch": time.time() + deadline_offset,
            "max_turns": max_turns,
            "max_tokens": max_tokens,
            "nudge_count": nudge,
            "empty_stops": 0,
            "task": task,
            "started_at": "2026-04-09T10:00:00+0800",
        }))
        return sf


class TestVersion(CLITestBase):
    def test_version(self):
        r = self.run_cli("version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("keep-working", r.stdout)


class TestListStatus(CLITestBase):

    def test_list_empty(self):
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("no sessions", r.stdout)

    def test_status_empty(self):
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("No active", r.stdout)

    def test_list_one_session(self):
        self.make_session("abc123", task="my work")
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("abc123", r.stdout)
        self.assertIn("my work", r.stdout)

    def test_status_shows_details(self):
        self.make_session("abc123", task="hello world", nudge=5)
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("abc123", r.stdout)
        self.assertIn("hello world", r.stdout)
        self.assertIn("5", r.stdout)  # nudge count

    def test_status_json_empty(self):
        r = self.run_cli("status", "--json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(data["sessions"], [])
        self.assertIsNone(data["pending"])

    def test_status_json_with_sessions(self):
        self.make_session("abc123", task="JSON output test", nudge=7)
        r = self.run_cli("status", "--json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(len(data["sessions"]), 1)
        s = data["sessions"][0]
        self.assertEqual(s["session_id"], "abc123")
        self.assertEqual(s["task"], "JSON output test")
        self.assertEqual(s["nudge_count"], 7)
        self.assertIn("remaining_sec", s)
        self.assertGreater(s["remaining_sec"], 0)

    def test_status_shows_pending_only(self):
        """Right after skill writes pending but before first tool call."""
        pending = self.claude / "keep-working-pending.json"
        pending.write_text(json.dumps({
            "active": True,
            "deadline_epoch": time.time() + 1800,
            "task": "pending-not-yet-bound",
            "created_at_epoch": time.time(),
        }))
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("PENDING", r.stdout)
        self.assertIn("pending-not-yet-bound", r.stdout)

    def test_status_json_pending_only(self):
        pending = self.claude / "keep-working-pending.json"
        pending.write_text(json.dumps({
            "active": True,
            "deadline_epoch": time.time() + 1800,
            "task": "pending-only",
            "created_at_epoch": time.time(),
        }))
        r = self.run_cli("status", "--json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertIsNotNone(data["pending"])
        self.assertEqual(data["pending"]["task"], "pending-only")
        self.assertEqual(data["sessions"], [])

    def test_status_shows_multiple(self):
        self.make_session("session-A", task="task A")
        self.make_session("session-B", task="task B")
        r = self.run_cli("status")
        self.assertIn("session-A", r.stdout)
        self.assertIn("session-B", r.stdout)


class TestStop(CLITestBase):

    def test_stop_fails_if_no_sessions(self):
        r = self.run_cli("stop")
        self.assertEqual(r.returncode, 1)
        flag = self.claude / "keep-working-stop-request"
        self.assertFalse(flag.exists())

    def test_stop_with_force_writes_flag(self):
        r = self.run_cli("stop", "--force")
        self.assertEqual(r.returncode, 0)
        flag = self.claude / "keep-working-stop-request"
        self.assertTrue(flag.exists())

    def test_stop_with_active_session_writes_flag(self):
        self.make_session("active-one")
        r = self.run_cli("stop")
        self.assertEqual(r.returncode, 0)
        flag = self.claude / "keep-working-stop-request"
        self.assertTrue(flag.exists())


class TestClean(CLITestBase):

    def test_clean_empty(self):
        r = self.run_cli("clean")
        self.assertEqual(r.returncode, 0)

    def test_clean_removes_sessions(self):
        self.make_session("s1")
        self.make_session("s2")
        (self.claude / "keep-working-pending.json").write_text("{}")
        (self.claude / "keep-working-stop-request").write_text("stop")
        r = self.run_cli("clean")
        self.assertEqual(r.returncode, 0)
        # All session files gone
        remaining = [p for p in self.sessions.glob("*.json")]
        self.assertEqual(remaining, [])
        self.assertFalse((self.claude / "keep-working-pending.json").exists())
        self.assertFalse((self.claude / "keep-working-stop-request").exists())


class TestExtend(CLITestBase):

    def test_extend_no_sessions_fails(self):
        r = self.run_cli("extend", "10")
        self.assertEqual(r.returncode, 1)

    def test_extend_all(self):
        self.make_session("s1", deadline_offset=60)
        self.make_session("s2", deadline_offset=60)
        r = self.run_cli("extend", "30")
        self.assertEqual(r.returncode, 0)
        # Both should now have deadline ~30 min later
        for sf in self.sessions.glob("*.json"):
            data = json.loads(sf.read_text())
            remaining = data["deadline_epoch"] - time.time()
            self.assertGreater(remaining, 29 * 60)
            self.assertLess(remaining, 31 * 60 + 5)

    def test_extend_specific_session(self):
        self.make_session("match-this", deadline_offset=60)
        self.make_session("leave-alone", deadline_offset=60)
        r = self.run_cli("extend", "30", "-s", "match")
        self.assertEqual(r.returncode, 0)
        for sf in self.sessions.glob("*.json"):
            data = json.loads(sf.read_text())
            remaining = data["deadline_epoch"] - time.time()
            if "match" in data["session_id"]:
                self.assertGreater(remaining, 29 * 60)
            else:
                self.assertLess(remaining, 2 * 60)

    def test_extend_no_match(self):
        self.make_session("something", deadline_offset=60)
        r = self.run_cli("extend", "10", "-s", "nomatch")
        self.assertEqual(r.returncode, 1)

    def test_shorten_with_negative(self):
        self.make_session("s1", deadline_offset=3600)
        r = self.run_cli("extend", "-30")
        self.assertEqual(r.returncode, 0)
        for sf in self.sessions.glob("*.json"):
            data = json.loads(sf.read_text())
            remaining = data["deadline_epoch"] - time.time()
            # 3600 - 1800 = 1800 ± few seconds
            self.assertGreater(remaining, 29 * 60)
            self.assertLess(remaining, 31 * 60)


class TestConfig(CLITestBase):

    def test_config_lists_all_vars(self):
        r = self.run_cli("config")
        self.assertEqual(r.returncode, 0)
        for name in [
            "KEEP_WORKING_NUDGE_CAP",
            "KEEP_WORKING_STAGNATION_CAP",
            "KEEP_WORKING_LOG_MAX_BYTES",
            "KEEP_WORKING_ORPHAN_TTL_SEC",
            "KEEP_WORKING_PENDING_TTL_SEC",
            "KEEP_WORKING_SCAN_MAX_BYTES",
            "CLAUDE_HOME",
        ]:
            self.assertIn(name, r.stdout)

    def test_config_shows_env_override(self):
        self.env["KEEP_WORKING_NUDGE_CAP"] = "777"
        r = self.run_cli("config")
        self.assertIn("777", r.stdout)


class TestLog(CLITestBase):

    def test_log_empty(self):
        r = self.run_cli("log")
        self.assertEqual(r.returncode, 0)
        self.assertIn("no log", r.stdout)

    def test_log_tail(self):
        log = self.sessions / "log.txt"
        log.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
        r = self.run_cli("log", "-n", "5")
        self.assertEqual(r.returncode, 0)
        self.assertIn("line 99", r.stdout)
        self.assertIn("line 95", r.stdout)
        self.assertNotIn("line 50", r.stdout)


class TestResume(CLITestBase):

    def test_resume_no_sessions(self):
        r = self.run_cli("resume")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No active", r.stderr + r.stdout)

    def test_resume_no_match(self):
        self.make_session("abc123", task="some task")
        r = self.run_cli("resume", "-s", "nomatch")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No active session matching", r.stderr + r.stdout)

    def test_resume_help_works(self):
        # We can't exec `claude` in tests, but --help shouldn't try to.
        r = self.run_cli("resume", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--session", r.stdout)
        self.assertIn("--latest", r.stdout)
        self.assertIn("--message", r.stdout)

    def test_resume_single_session_missing_claude(self):
        """If only one active session but `claude` CLI is missing, fail
        clearly instead of exec'ing garbage."""
        self.make_session("only-one", task="the task")
        # Keep python3 discoverable but scrub anything that might be `claude`.
        # Point PATH at a temp empty dir alongside the system python paths.
        import shutil as _sh
        python_dir = str(Path(_sh.which("python3")).parent) if _sh.which("python3") else "/usr/bin"
        empty_dir = self.sandbox / "empty_bin"
        empty_dir.mkdir()
        self.env["PATH"] = f"{python_dir}:{empty_dir}"
        # Confirm `claude` is now hidden
        if _sh.which("claude", path=self.env["PATH"]):
            self.skipTest("Could not scrub claude from test PATH")
        r = self.run_cli("resume")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr + r.stdout)


class TestDoctor(CLITestBase):

    def test_doctor_fresh_sandbox_fails_gracefully(self):
        # No hook script, no settings.json, etc. Doctor should report problems
        # but not crash.
        r = self.run_cli("doctor")
        # It will complain about missing files but exit 1, not crash
        self.assertIn(r.returncode, (0, 1))
        self.assertTrue(r.stdout)

    def test_doctor_quiet_suppresses_output(self):
        r = self.run_cli("doctor", "--quiet")
        self.assertIn(r.returncode, (0, 1))
        self.assertEqual(r.stdout.strip(), "")

    def test_doctor_short_quiet_flag(self):
        r = self.run_cli("doctor", "-q")
        self.assertIn(r.returncode, (0, 1))
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
