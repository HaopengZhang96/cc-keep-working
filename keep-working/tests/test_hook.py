#!/usr/bin/env python3
"""
Test suite for keep-working hook.

Run with:
    python3 tests/test_hook.py

The tests use a sandboxed CLAUDE_HOME, so they don't touch your real
~/.claude/ at all. Run anywhere.
"""
from __future__ import annotations

import hashlib
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
HOOK = REPO / "hooks" / "keep-working.py"
assert HOOK.exists(), f"hook not found at {HOOK}"


def session_filename(sid: str, sessions_dir: Path) -> Path:
    h = hashlib.sha1(sid.encode()).hexdigest()[:16]
    import re
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", sid)[:32]
    return sessions_dir / f"{safe}_{h}.json"


def make_transcript(tmpdir: Path, *messages):
    """messages: list of (tool_use_count, input_tokens) tuples."""
    p = tmpdir / f"t-{time.time_ns()}.jsonl"
    with open(p, "w") as f:
        for tu, it in messages:
            for i in range(tu):
                f.write(json.dumps({
                    "message": {
                        "content": [{"type": "tool_use", "id": f"t{i}"}],
                        "usage": {"input_tokens": it, "output_tokens": 0},
                    }
                }) + "\n")
    return str(p)


class HookTestBase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="kw-test-"))
        self.env = os.environ.copy()
        # Hook resolves Claude dir via CLAUDE_HOME or ~/.claude.
        # Override both for a fully sandboxed test.
        self.env["HOME"] = str(self.sandbox)
        self.env.pop("CLAUDE_HOME", None)
        self.claude = self.sandbox / ".claude"
        self.sessions = self.claude / "keep-working"
        self.pending = self.claude / "keep-working-pending.json"
        self.stop_request = self.claude / "keep-working-stop-request"
        self.claude.mkdir(parents=True, exist_ok=True)
        self.sessions.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def run_hook(self, sub, payload):
        r = subprocess.run(
            ["python3", str(HOOK), sub],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
        )
        return r.returncode, r.stderr, r.stdout

    def write_pending(self, **kwargs):
        defaults = {
            "active": True,
            "deadline_epoch": time.time() + 3600,
            "max_turns": 200,
            "max_tokens": 5_000_000,
            "task": "test",
            "created_at_epoch": time.time(),
        }
        defaults.update(kwargs)
        self.pending.write_text(json.dumps(defaults))

    def session_file_for(self, sid):
        return session_filename(sid, self.sessions)


class TestBind(HookTestBase):

    def test_no_pending_no_op(self):
        code, _, _ = self.run_hook("bind", {"session_id": "sid-A"})
        self.assertEqual(code, 0)
        self.assertFalse(any(self.sessions.glob("sid-A*.json")))

    def test_claims_pending(self):
        self.write_pending(task="claim-test")
        code, _, _ = self.run_hook("bind", {"session_id": "sid-A"})
        self.assertEqual(code, 0)
        self.assertFalse(self.pending.exists())
        sf = self.session_file_for("sid-A")
        self.assertTrue(sf.exists())
        state = json.loads(sf.read_text())
        self.assertEqual(state["session_id"], "sid-A")
        self.assertEqual(state["task"], "claim-test")
        self.assertEqual(state["nudge_count"], 0)

    def test_no_session_id_no_op(self):
        self.write_pending()
        code, _, _ = self.run_hook("bind", {})
        self.assertEqual(code, 0)
        # Pending should still be there
        self.assertTrue(self.pending.exists())

    def test_stale_pending_dropped(self):
        self.write_pending(created_at_epoch=time.time() - 99999)
        self.run_hook("bind", {"session_id": "sid-X"})
        self.assertFalse(self.pending.exists())
        self.assertFalse(any(self.sessions.glob("sid-X*.json")))

    def test_corrupt_pending_dropped(self):
        self.pending.write_text("not json {{")
        self.run_hook("bind", {"session_id": "sid-Y"})
        self.assertFalse(self.pending.exists())

    def test_existing_session_not_clobbered_by_older_pending(self):
        """If state file's bound_at_epoch is MORE RECENT than pending's
        created_at_epoch, the pending is ignored (not a new request)."""
        # Write an "old" pending (created 10s ago)
        self.pending.write_text(json.dumps({
            "active": True,
            "deadline_epoch": time.time() + 3600,
            "task": "stale-pending",
            "created_at_epoch": time.time() - 10,
        }))
        # Write a "newer" state file (bound just now)
        sf = self.session_file_for("sid-Z")
        sf.write_text(json.dumps({
            "active": True,
            "task": "current-task",
            "session_id": "sid-Z",
            "bound_at_epoch": time.time(),
        }))
        self.run_hook("bind", {"session_id": "sid-Z"})
        # pending stays untouched, state unchanged
        self.assertTrue(self.pending.exists())
        self.assertEqual(json.loads(sf.read_text())["task"], "current-task")

    def test_orphan_sweep(self):
        old = self.sessions / "orphan_dead.json"
        old.write_text("{}")
        os.utime(old, (time.time() - 99999, time.time() - 99999))
        self.run_hook("bind", {"session_id": "sid-O"})
        self.assertFalse(old.exists())

    def test_path_traversal_blocked(self):
        self.write_pending()
        self.run_hook("bind", {"session_id": "../../../etc/passwd"})
        # Nothing created outside SESSIONS dir
        self.assertFalse((self.sandbox / "etc" / "passwd.json").exists())

    def test_stop_request_flag(self):
        self.write_pending()
        self.run_hook("bind", {"session_id": "sid-S"})
        self.assertTrue(any(self.sessions.glob("sid-S*.json")))
        self.stop_request.write_text("stop")
        self.run_hook("bind", {"session_id": "sid-S"})
        self.assertFalse(self.stop_request.exists())
        self.assertFalse(any(self.sessions.glob("sid-S*.json")))

    def test_same_session_reclaim_newer_pending(self):
        """If the user starts a NEW keep-working request in the SAME session
        (e.g. after context exhaustion killed the previous run), the newer
        pending should replace the old state file."""
        # First keep-working session
        self.write_pending(task="old task")
        self.run_hook("bind", {"session_id": "sid-R"})
        sf = next(self.sessions.glob("sid-R*.json"))
        old_state = json.loads(sf.read_text())
        self.assertEqual(old_state["task"], "old task")

        # Simulate time passing, then user starts a new keep-working request
        time.sleep(0.1)
        self.write_pending(task="new task")
        self.run_hook("bind", {"session_id": "sid-R"})
        new_state = json.loads(sf.read_text())
        self.assertEqual(new_state["task"], "new task")
        self.assertFalse(self.pending.exists(), "pending should be consumed")

    def test_same_session_older_pending_ignored(self):
        """If the pending file is OLDER than the current state (race
        condition or stale file), don't replace."""
        # Write pending first
        self.write_pending(task="stale pending")
        time.sleep(0.1)
        # Then bind — state file's bound_at_epoch > pending's created_at_epoch
        self.run_hook("bind", {"session_id": "sid-O"})
        sf = next(self.sessions.glob("sid-O*.json"))

        # Write another pending that is OLDER (by faking created_at_epoch)
        self.pending.write_text(json.dumps({
            "active": True,
            "deadline_epoch": time.time() + 3600,
            "task": "older pending",
            "created_at_epoch": time.time() - 9999,
        }))
        self.run_hook("bind", {"session_id": "sid-O"})
        state = json.loads(sf.read_text())
        self.assertEqual(state["task"], "stale pending")  # unchanged

    def test_insane_deadline_clamped(self):
        """A pending file with a 100-year deadline should be clamped to
        a sane horizon (default 25h)."""
        hundred_years = time.time() + 100 * 365 * 86400
        self.write_pending(deadline_epoch=hundred_years)
        self.run_hook("bind", {"session_id": "sid-crazy"})
        sf = next(self.sessions.glob("sid-crazy*.json"))
        state = json.loads(sf.read_text())
        # Clamped to ~25 hours
        self.assertLess(state["deadline_epoch"], time.time() + 26 * 3600)
        self.assertGreater(state["deadline_epoch"], time.time() + 24 * 3600)

    def test_concurrent_bind_atomic(self):
        """20 parallel bind calls on the same pending file must result in
        exactly one winner. Regression for the bind race fix using
        os.rename as the atomic primitive."""
        import threading
        self.write_pending(task="race-test")
        results = []

        def bind(sid):
            code, _, _ = self.run_hook("bind", {"session_id": sid})
            results.append(code)

        threads = [threading.Thread(target=bind, args=(f"sid-{i}",))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(c == 0 for c in results))
        self.assertFalse(self.pending.exists())
        # Exactly one session file should exist
        sf = list(self.sessions.glob("sid-*.json"))
        self.assertEqual(len(sf), 1, f"expected 1 winner, got {len(sf)}")

    def test_hook_version_flag(self):
        """Hook supports --version / -V / version subcommand."""
        for arg in ("--version", "-V", "version"):
            r = subprocess.run(
                ["python3", str(HOOK), arg],
                input="", capture_output=True, text=True, env=self.env,
            )
            self.assertEqual(r.returncode, 0, f"arg={arg}")
            self.assertIn("keep-working hook", r.stdout)
            self.assertIn("0.2.0", r.stdout)

    def test_bind_never_blocks_tool_call(self):
        # Even with corrupt state files, bind must exit 0.
        self.pending.write_text("{{{ corrupt")
        code, _, _ = self.run_hook("bind", {"session_id": "sid-X"})
        self.assertEqual(code, 0)


class TestStop(HookTestBase):

    def test_no_state_allows_stop(self):
        code, _, _ = self.run_hook("stop", {"session_id": "sid-A", "transcript_path": "/x"})
        self.assertEqual(code, 0)

    def test_recursion_guard(self):
        self.write_pending()
        self.run_hook("bind", {"session_id": "sid-A"})
        code, _, _ = self.run_hook("stop", {
            "session_id": "sid-A",
            "stop_hook_active": True,
            "transcript_path": "/x",
        })
        self.assertEqual(code, 0)

    def test_active_session_blocks(self):
        self.write_pending(task="block-me")
        self.run_hook("bind", {"session_id": "sid-A"})
        tr = make_transcript(self.sandbox, (1, 1000))
        code, err, _ = self.run_hook("stop", {"session_id": "sid-A", "transcript_path": tr})
        self.assertEqual(code, 2)
        self.assertIn("block-me", err)
        self.assertIn("DO NOT STOP", err)
        # Should NOT be JSON-wrapped — see _emit_block docstring.
        self.assertNotIn('"decision"', err)

    def test_unrelated_session_allowed(self):
        self.write_pending()
        self.run_hook("bind", {"session_id": "sid-A"})
        code, _, _ = self.run_hook("stop", {"session_id": "sid-B", "transcript_path": "/x"})
        self.assertEqual(code, 0)

    def test_deadline_passed_releases(self):
        self.write_pending(deadline_epoch=time.time() - 1)
        self.run_hook("bind", {"session_id": "sid-D"})
        tr = make_transcript(self.sandbox, (1, 1000))
        code, _, _ = self.run_hook("stop", {"session_id": "sid-D", "transcript_path": tr})
        self.assertEqual(code, 0)
        self.assertFalse(any(self.sessions.glob("sid-D*.json")))

    def test_turn_cap_releases(self):
        self.write_pending(max_turns=2)
        self.run_hook("bind", {"session_id": "sid-T"})
        tr = make_transcript(self.sandbox, (1, 1000))
        c1, _, _ = self.run_hook("stop", {"session_id": "sid-T", "transcript_path": tr})
        tr2 = make_transcript(self.sandbox, (2, 1000))
        c2, _, _ = self.run_hook("stop", {"session_id": "sid-T", "transcript_path": tr2})
        tr3 = make_transcript(self.sandbox, (3, 1000))
        c3, _, _ = self.run_hook("stop", {"session_id": "sid-T", "transcript_path": tr3})
        self.assertEqual((c1, c2, c3), (2, 2, 0))

    def test_token_cap_uses_max_not_sum(self):
        # 20 messages each with 5k tokens. SUM=100k, MAX=5k. Cap=50k.
        # Should NOT release (max-not-sum semantics).
        self.write_pending(max_tokens=50_000)
        self.run_hook("bind", {"session_id": "sid-T"})
        tr = make_transcript(self.sandbox, *[(1, 5000)] * 20)
        code, _, _ = self.run_hook("stop", {"session_id": "sid-T", "transcript_path": tr})
        self.assertEqual(code, 2)

    def test_token_cap_releases_on_big_message(self):
        self.write_pending(max_tokens=50_000)
        self.run_hook("bind", {"session_id": "sid-T2"})
        tr = make_transcript(self.sandbox, (1, 100_000))
        code, _, _ = self.run_hook("stop", {"session_id": "sid-T2", "transcript_path": tr})
        self.assertEqual(code, 0)

    def _append_tool_use(self, path: Path, count: int = 1, input_tokens: int = 1000):
        with open(path, "a") as f:
            for i in range(count):
                f.write(json.dumps({
                    "message": {
                        "content": [{"type": "tool_use", "id": f"t-{time.time_ns()}-{i}"}],
                        "usage": {"input_tokens": input_tokens},
                    }
                }) + "\n")

    def _append_text(self, path: Path, text: str = "no work"):
        """Append an assistant turn with only text, no tool_use."""
        with open(path, "a") as f:
            f.write(json.dumps({
                "message": {
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 500},
                }
            }) + "\n")

    def test_stagnation_detection_text_only_turns(self):
        """3 consecutive assistant turns with NO tool_use → release."""
        self.write_pending()
        self.run_hook("bind", {"session_id": "sid-S"})
        tr = self.sandbox / "transcript.jsonl"
        # Initial: one tool_use (normal work)
        self._append_tool_use(tr)
        codes = []

        # Stop 1: delta has 1 tool_use → progress → empty_stops=0, block
        c, _, _ = self.run_hook("stop", {"session_id": "sid-S", "transcript_path": str(tr)})
        codes.append(c)
        # Append text-only turn (Claude "done" narration)
        self._append_text(tr)
        # Stop 2: delta has 0 tool_use → empty_stops=1, block
        c, _, _ = self.run_hook("stop", {"session_id": "sid-S", "transcript_path": str(tr)})
        codes.append(c)
        self._append_text(tr)
        # Stop 3: empty_stops=2, block
        c, _, _ = self.run_hook("stop", {"session_id": "sid-S", "transcript_path": str(tr)})
        codes.append(c)
        self._append_text(tr)
        # Stop 4: empty_stops=3 = STAGNATION_CAP → release
        c, _, _ = self.run_hook("stop", {"session_id": "sid-S", "transcript_path": str(tr)})
        codes.append(c)
        self.assertEqual(codes, [2, 2, 2, 0])

    def test_progress_resets_stagnation(self):
        self.write_pending()
        self.run_hook("bind", {"session_id": "sid-P"})
        tr = self.sandbox / "transcript.jsonl"
        self._append_tool_use(tr)

        # Pattern: stop (work), stop (text), stop (work), stop (text), ...
        # Each "work" resets empty_stops to 0, so we never hit the cap.
        for i in range(6):
            code, _, _ = self.run_hook(
                "stop", {"session_id": "sid-P", "transcript_path": str(tr)}
            )
            self.assertEqual(code, 2, f"iter {i}")
            if i % 2 == 0:
                self._append_text(tr)
            else:
                self._append_tool_use(tr)

    def test_stagnation_survives_large_transcript(self):
        """Delta scan must stay correct even on a huge pre-existing
        transcript. Regression for the scan-window-slide bug."""
        self.write_pending()
        self.run_hook("bind", {"session_id": "sid-W"})
        tr = self.sandbox / "transcript.jsonl"
        # Pre-populate 10MB of irrelevant content
        junk = json.dumps({"message": {"content": [{"type": "text", "text": "x" * 800}]}})
        with open(tr, "w") as f:
            for _ in range(12_000):
                f.write(junk + "\n")
        self.assertGreater(tr.stat().st_size, 8_000_000)
        # Bind reads nothing. First stop scans from 0 but capped at SCAN_MAX_BYTES
        # from the end. Still no tool_use in that window → empty_stops=1.
        c1, _, _ = self.run_hook("stop", {"session_id": "sid-W", "transcript_path": str(tr)})
        self.assertEqual(c1, 2)
        # Append real tool_use — delta scan sees it → progress.
        self._append_tool_use(tr)
        c2, _, _ = self.run_hook("stop", {"session_id": "sid-W", "transcript_path": str(tr)})
        self.assertEqual(c2, 2)
        # Read state to check empty_stops reset
        sf = next(self.sessions.glob("sid-W*.json"))
        state = json.loads(sf.read_text())
        self.assertEqual(state["empty_stops"], 0)

    def test_corrupt_session_file_removed(self):
        sf = self.session_file_for("sid-Z")
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text("not json")
        code, _, _ = self.run_hook("stop", {"session_id": "sid-Z", "transcript_path": "/x"})
        self.assertEqual(code, 0)
        self.assertFalse(sf.exists())

    def test_inactive_state_allows(self):
        sf = self.session_file_for("sid-I")
        sf.write_text(json.dumps({"active": False, "session_id": "sid-I"}))
        code, _, _ = self.run_hook("stop", {"session_id": "sid-I", "transcript_path": "/x"})
        self.assertEqual(code, 0)
        self.assertFalse(sf.exists())

    def test_state_file_chmod_600(self):
        self.write_pending()
        self.run_hook("bind", {"session_id": "sid-A"})
        sf = next(self.sessions.glob("sid-A*.json"))
        mode = oct(sf.stat().st_mode & 0o777)
        self.assertEqual(mode, "0o600")


class TestI18n(HookTestBase):

    def test_chinese_task_chinese_reason(self):
        self.write_pending(task="重构 auth 模块")
        self.run_hook("bind", {"session_id": "cn"})
        tr = make_transcript(self.sandbox, (1, 100))
        code, err, _ = self.run_hook("stop", {"session_id": "cn", "transcript_path": tr})
        self.assertEqual(code, 2)
        self.assertIn("不要停止", err)
        self.assertIn("剩余", err)
        self.assertNotIn("DO NOT STOP", err)

    def test_english_task_english_reason(self):
        self.write_pending(task="refactor auth module")
        self.run_hook("bind", {"session_id": "en"})
        tr = make_transcript(self.sandbox, (1, 100))
        code, err, _ = self.run_hook("stop", {"session_id": "en", "transcript_path": tr})
        self.assertEqual(code, 2)
        self.assertIn("DO NOT STOP", err)
        self.assertNotIn("不要停止", err)


class TestTranscriptScan(HookTestBase):

    def test_big_transcript_streaming(self):
        """A 10MB+ transcript must scan in well under a second."""
        import time as _t
        p = self.sandbox / "big.jsonl"
        junk = json.dumps({"message": {"content": [{"type": "text", "text": "x" * 500}]}})
        with open(p, "w") as f:
            for _ in range(20_000):
                f.write(junk + "\n")
            # Last line: legit usage and tool_use
            f.write(json.dumps({
                "message": {
                    "content": [{"type": "tool_use", "id": "t"}],
                    "usage": {"input_tokens": 12345},
                }
            }) + "\n")
        self.assertGreater(p.stat().st_size, 8_000_000)
        self.write_pending(max_tokens=100_000)
        self.run_hook("bind", {"session_id": "big"})
        t0 = _t.time()
        code, _, _ = self.run_hook(
            "stop", {"session_id": "big", "transcript_path": str(p)}
        )
        dt = _t.time() - t0
        self.assertEqual(code, 2)  # 12345 < 100000, should block
        self.assertLess(dt, 1.0, f"scan too slow: {dt}s")

    def test_missing_transcript_safe(self):
        self.write_pending()
        self.run_hook("bind", {"session_id": "nope"})
        code, _, _ = self.run_hook(
            "stop", {"session_id": "nope", "transcript_path": "/does/not/exist"}
        )
        self.assertEqual(code, 2)  # still blocks — missing transcript just means ctx=0


class TestEnvVars(HookTestBase):

    def test_stagnation_cap_env_override(self):
        self.env["KEEP_WORKING_STAGNATION_CAP"] = "1"
        self.write_pending()
        self.run_hook("bind", {"session_id": "sid-A"})
        tr = self.sandbox / "transcript.jsonl"
        # Start with a tool_use
        with open(tr, "w") as f:
            f.write(json.dumps({
                "message": {
                    "content": [{"type": "tool_use", "id": "t"}],
                    "usage": {"input_tokens": 1000},
                }
            }) + "\n")
        # Stop 1: delta has tool_use → progress → empty_stops=0, block
        c1, _, _ = self.run_hook("stop", {"session_id": "sid-A", "transcript_path": str(tr)})
        # Append text-only turn
        with open(tr, "a") as f:
            f.write(json.dumps({"message": {"content": [{"type": "text", "text": "done"}]}}) + "\n")
        # Stop 2: delta has no tool_use → empty_stops=1, cap=1, release
        c2, _, _ = self.run_hook("stop", {"session_id": "sid-A", "transcript_path": str(tr)})
        self.assertEqual((c1, c2), (2, 0))

    def test_max_horizon_env_override(self):
        """KEEP_WORKING_MAX_HORIZON_SEC should clamp insane deadlines
        to a smaller window when set."""
        self.env["KEEP_WORKING_MAX_HORIZON_SEC"] = "3600"  # 1 hour
        # Request 10-hour deadline
        self.write_pending(deadline_epoch=time.time() + 10 * 3600)
        self.run_hook("bind", {"session_id": "sid-H"})
        sf = next(self.sessions.glob("sid-H*.json"))
        state = json.loads(sf.read_text())
        remaining = state["deadline_epoch"] - time.time()
        self.assertLess(remaining, 3610)  # ≤ 1h + small fudge
        self.assertGreater(remaining, 3500)

    def test_orphan_ttl_env_override(self):
        self.env["KEEP_WORKING_ORPHAN_TTL_SEC"] = "10"
        old = self.sessions / "orphan.json"
        old.write_text("{}")
        os.utime(old, (time.time() - 100, time.time() - 100))
        self.run_hook("bind", {"session_id": "sid-O"})
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
