#!/usr/bin/env python3
"""
keep-working watchdog — background heartbeat monitor with auto-recovery.

Detects when a keep-working session has stalled (Claude Code process
died, API timed out, network dropped) by monitoring transcript file
growth. If no growth for WATCHDOG_STALL_MIN minutes, fires a desktop
notification. After a confirmation cycle, automatically resumes the
session via `claude --resume <session-id>`.

Usage:
    python3 watchdog.py start           # daemonize, write PID file
    python3 watchdog.py stop            # kill the daemon
    python3 watchdog.py status          # check if running
    python3 watchdog.py run             # foreground (for debugging)
    python3 watchdog.py recover <sid>   # manually recover a session

The watchdog checks every WATCHDOG_INTERVAL_SEC seconds (default 60).
If the transcript file hasn't grown in WATCHDOG_STALL_MIN minutes
(default 5), it considers the session stalled.

Env vars:
    WATCHDOG_INTERVAL_SEC           Check interval (default 60)
    WATCHDOG_STALL_MIN              Minutes without growth = stalled (default 5)
    WATCHDOG_AUTO_RECOVER           Enable auto-recovery (default 1)
    WATCHDOG_RECOVER_MAX            Max recovery attempts per stall (default 3)
    WATCHDOG_RECOVER_COOLDOWN_MIN   Minutes between attempts (default 3)
    CLAUDE_HOME                     Override ~/.claude
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
SESSIONS_DIR = CLAUDE_DIR / "keep-working"
PID_FILE = SESSIONS_DIR / "watchdog.pid"
LOG_FILE = SESSIONS_DIR / "log.txt"

INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL_SEC", "") or 60)
# Raised from 5 to 10 min — long tool calls (big Grep, slow Bash, heavy Read)
# can legitimately take 5-8 min without the transcript advancing, especially
# at high context. 5 min was producing >70% false-positive stall events.
STALL_MIN = int(os.environ.get("WATCHDOG_STALL_MIN", "") or 10)
# Default ON. Recovery now opens a NEW Terminal.app window running an
# INTERACTIVE `claude --resume <sid> "继续"` (see _recover_session). The
# new window loads settings.json hooks so the keep-working loop resumes
# naturally. The old `-p`/non-interactive approach (removed) didn't work.
AUTO_RECOVER = os.environ.get("WATCHDOG_AUTO_RECOVER", "1") not in ("0", "false", "no")
RECOVER_MAX = int(os.environ.get("WATCHDOG_RECOVER_MAX", "") or 3)
RECOVER_COOLDOWN_MIN = int(os.environ.get("WATCHDOG_RECOVER_COOLDOWN_MIN", "") or 5)
# Max minutes we'll suppress a stall notification on the "claude process
# alive" heuristic. Past this, even with a live process, transcript idleness
# means something is stuck (API hang, network issue, model hung) and the
# user needs to know. Empirically, Claude Code has been observed idle for
# 69+ min with the process alive but no transcript activity.
STALL_SUPPRESS_MAX_MIN = int(os.environ.get("WATCHDOG_STALL_SUPPRESS_MAX_MIN", "") or 20)
# Don't log "STALL suppressed" on every check cycle — every 5 min is plenty.
STALL_SUPPRESS_LOG_EVERY_MIN = 5


def _log(msg: str) -> None:
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} watchdog: {msg}\n"
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def _notify(title: str, body: str) -> None:
    """Send a macOS desktop notification. Falls back to terminal bell."""
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{body}" with title "{title}" sound name "Basso"'
        ], timeout=5, capture_output=True)
    except Exception:
        # Fallback: terminal bell
        sys.stderr.write(f"\a[watchdog] {title}: {body}\n")


def _session_process_alive(session_id: str) -> bool | None:
    """Check whether THIS session's claude process is running.

    Returns True if we found a `claude` process whose command line
    contains `--resume <session_id>` or `<session_id>` as an argument.
    Returns False if we could enumerate processes and found no match.
    Returns None if we couldn't enumerate (pgrep/ps unavailable).

    Previously we checked "any claude process alive" which caused false
    negatives when the user had multiple Claude Code windows open: a
    dead session's stall would be suppressed because a DIFFERENT live
    session's process existed.
    """
    if not session_id:
        return None
    try:
        # `ps -Ao args=` gives each process's full command line. We look for
        # lines that (a) reference the Claude Code binary AND (b) contain
        # this session_id. This avoids shells/editors whose args happen to
        # contain both "claude" and the sid.
        # Use bytes mode to tolerate non-UTF-8 in other processes' args.
        r = subprocess.run(
            ["ps", "-Ao", "args="],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        stdout = r.stdout.decode("utf-8", errors="replace")
        # Signatures that reliably identify the Claude Code CLI binary.
        # Strict enough to exclude random processes that mention the sid.
        claude_markers = ("claude-code/", "/claude.app/Contents/MacOS/claude")
        for line in stdout.splitlines():
            if session_id not in line:
                continue
            if any(m in line for m in claude_markers):
                return True
        return False
    except Exception:
        return None


def _any_claude_process_alive() -> bool:
    """Legacy fallback: whether ANY claude process is running. Kept for
    callers that don't have a session_id available."""
    try:
        r = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return True


def _find_transcript(session_id: str) -> str | None:
    """Find the transcript JSONL for a session by looking through
    Claude Code project directories."""
    projects = CLAUDE_DIR / "projects"
    if not projects.exists():
        return None
    for proj in projects.iterdir():
        if not proj.is_dir():
            continue
        candidate = proj / f"{session_id}.jsonl"
        if candidate.exists():
            return str(candidate)
    return None


def _load_active_sessions() -> list[dict]:
    out = []
    if not SESSIONS_DIR.exists():
        return out
    for p in SESSIONS_DIR.iterdir():
        if not p.name.endswith(".json"):
            continue
        try:
            data = json.loads(p.read_text())
            if data.get("active"):
                data["_path"] = str(p)
                out.append(data)
        except Exception:
            continue
    return out


# --- Stall marker I/O ---

def _stall_marker_path(sid: str) -> Path:
    return SESSIONS_DIR / f".stall-{sid[:16]}"


def _read_stall_marker(sid: str) -> dict | None:
    """Read stall marker. Returns dict or None if no marker."""
    path = _stall_marker_path(sid)
    if not path.exists():
        return None
    try:
        text = path.read_text().strip()
        # Migrate old format (plain epoch string) to new JSON
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        # Old format: just an epoch float
        return {
            "detected_at": float(text),
            "recovery_attempts": 0,
            "last_recovery_at": None,
            "unrecoverable": False,
            "recovered_pids": [],
        }
    except Exception:
        return None


def _write_stall_marker(sid: str, data: dict) -> None:
    try:
        _stall_marker_path(sid).write_text(json.dumps(data))
    except Exception:
        pass


def _clear_stall_marker(sid: str) -> None:
    try:
        _stall_marker_path(sid).unlink()
    except Exception:
        pass


# --- Auto-recovery ---

def _shell_quote_applescript(s: str) -> str:
    """Quote for embedding inside an AppleScript `do script` string literal."""
    # AppleScript string literals use double quotes; backslash + quote escape.
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _recover_session(sid: str, state: dict, stall_data: dict) -> bool:
    """Open a NEW macOS Terminal window running `claude --resume <sid>`
    with an initial "继续工作" prompt. The old `-p` approach didn't work
    because print-mode is one-shot; we need an interactive session so
    the keep-working Stop hook loop can resume.

    Returns True if recovery was triggered. Requires macOS (Terminal.app +
    osascript). On other platforms, logs and skips.
    """
    try:
        # Detect macOS — osascript + Terminal.app is macOS-only.
        if sys.platform != "darwin":
            _log(f"RECOVER SKIPPED sid={sid[:8]} — auto-recovery currently macOS-only")
            _notify(
                "keep-working: auto-recover skipped",
                f"Session {sid[:8]} stalled, platform={sys.platform} not supported for auto-recovery. Use `keep-working resume`.",
            )
            return False

        import shutil as _sh
        if not _sh.which("claude"):
            _log(f"RECOVER FAILED sid={sid[:8]} — 'claude' not found in PATH")
            _notify("keep-working: recovery failed", "claude CLI not found in PATH")
            return False

        task = (state.get("task", "") or "")[:40]
        initial = os.environ.get("WATCHDOG_RECOVER_PROMPT") or "继续按原计划工作"

        # Build the interactive shell command to run in the new Terminal tab.
        # We source the user's shell profile so `claude` is on PATH.
        shell_cmd = f'claude --resume {sid} "{_shell_quote_applescript(initial)}"'

        # AppleScript: open Terminal, run the command in a new window.
        applescript = (
            'tell application "Terminal"\n'
            '  activate\n'
            f'  do script "{_shell_quote_applescript(shell_cmd)}"\n'
            'end tell'
        )

        proc = subprocess.Popen(
            ["osascript", "-e", applescript],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        # Give osascript a moment to dispatch (don't wait forever).
        try:
            _, stderr_bytes = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _log(f"RECOVER TIMEOUT sid={sid[:8]} — osascript hung")
            return False

        if proc.returncode != 0:
            err = stderr_bytes.decode("utf-8", errors="replace").strip()[:200]
            _log(f"RECOVER FAILED sid={sid[:8]} — osascript rc={proc.returncode} err={err!r}")
            _notify("keep-working: recovery failed", f"osascript error: {err[:100]}")
            return False

        stall_data["recovery_attempts"] = stall_data.get("recovery_attempts", 0) + 1
        stall_data["last_recovery_at"] = time.time()
        stall_data.setdefault("recovered_pids", []).append(proc.pid)

        attempt = stall_data["recovery_attempts"]
        _log(f"RECOVER attempt {attempt}/{RECOVER_MAX} sid={sid[:8]} — opened new Terminal window; task={task}")
        _notify(
            "keep-working: auto-recovering",
            f"Session {sid[:8]} — opened new Terminal with `claude --resume` (attempt {attempt}/{RECOVER_MAX})",
        )
        return True

    except Exception as e:
        _log(f"RECOVER FAILED sid={sid[:8]} — {e}")
        return False


# Per-session state used only within the running daemon (not persisted).
# Tracks the last "age bucket" (units of STALL_SUPPRESS_LOG_EVERY_MIN
# minutes) we logged for a suppressed stall, so we log at most once per
# bucket — avoids 70 log lines / hour during long tool calls.
_bucket_tracker: dict[str, int] = {}


def _check_once() -> None:
    """One watchdog check cycle."""
    sessions = _load_active_sessions()
    now = time.time()

    for s in sessions:
        sid = s.get("session_id", "?")
        deadline = float(s.get("deadline_epoch", 0) or 0)

        # Skip expired sessions
        if deadline and now > deadline:
            continue

        # Find the transcript file
        transcript = _find_transcript(sid)
        if not transcript:
            continue

        try:
            mtime = os.path.getmtime(transcript)
        except OSError:
            continue

        age_min = (now - mtime) / 60
        stall_data = _read_stall_marker(sid)

        if age_min > STALL_MIN:
            # Prefer per-session check; fall back to any-claude when unknown.
            sess_alive = _session_process_alive(sid)
            if sess_alive is None:
                claude_alive = _any_claude_process_alive()
            else:
                claude_alive = sess_alive

            if stall_data is None:
                # --- First detection ---
                # Suppress the alert only for SHORT idleness (< STALL_SUPPRESS_MAX_MIN)
                # when claude is alive. Long idleness is never a legitimate tool call.
                if claude_alive and age_min < STALL_SUPPRESS_MAX_MIN:
                    # Log at most every STALL_SUPPRESS_LOG_EVERY_MIN minutes.
                    minute_bucket = int(age_min) // STALL_SUPPRESS_LOG_EVERY_MIN
                    last_bucket = _bucket_tracker.get(sid, -1)
                    if minute_bucket != last_bucket:
                        _log(f"STALL suppressed sid={sid[:8]} age={int(age_min)}min — claude process alive (short stall, probably long tool call)")
                        _bucket_tracker[sid] = minute_bucket
                    continue

                task = (s.get("task", "") or "")[:60]
                remaining = max(0, int((deadline - now) / 60)) if deadline else -1
                rem_str = f"{remaining}min left" if remaining >= 0 else "no deadline"
                if claude_alive:
                    title = "keep-working: session possibly hanging"
                    body = (
                        f"Session {sid[:8]} — transcript idle for {int(age_min)} min "
                        f"but claude process still alive (likely API hang or network issue). "
                        f"Task: {task}... ({rem_str}). Consider Ctrl+C and re-resume."
                    )
                    detect_reason = f"long-hang (claude alive, idle {int(age_min)}min > suppress-max {STALL_SUPPRESS_MAX_MIN}min)"
                else:
                    title = "keep-working: session stalled!"
                    body = (
                        f"Session {sid[:8]} stalled — no activity for {int(age_min)} min, "
                        f"no claude process running. Task: {task}... ({rem_str})"
                    )
                    detect_reason = "no claude process"
                if AUTO_RECOVER:
                    body += " Auto-recovery in ~1 cycle."
                _notify(title, body)
                _log(f"STALL detected sid={sid[:8]} age={int(age_min)}min ({detect_reason}) transcript={transcript}")
                _write_stall_marker(sid, {
                    "detected_at": now,
                    "recovery_attempts": 0,
                    "last_recovery_at": None,
                    "unrecoverable": False,
                    "recovered_pids": [],
                    "claude_alive_on_detect": claude_alive,
                })
            elif not AUTO_RECOVER:
                # Auto-recovery disabled — notification only (already sent)
                pass
            elif stall_data.get("unrecoverable"):
                # Already gave up on this session
                pass
            elif stall_data.get("recovery_attempts", 0) >= RECOVER_MAX:
                # Max attempts exhausted — mark unrecoverable
                stall_data["unrecoverable"] = True
                _write_stall_marker(sid, stall_data)
                _log(f"UNRECOVERABLE sid={sid[:8]} after {RECOVER_MAX} attempts")
                _notify(
                    "keep-working: recovery failed",
                    f"Session {sid[:8]} unrecoverable after {RECOVER_MAX} attempts. Manual intervention needed.",
                )
            else:
                # Check cooldown
                last_attempt = stall_data.get("last_recovery_at") or 0
                cooldown_elapsed = (now - last_attempt) / 60 >= RECOVER_COOLDOWN_MIN
                # First recovery requires confirmation cycle (stall persisted across 2+ checks)
                first_cycle_ok = (now - stall_data.get("detected_at", now)) / 60 >= STALL_MIN
                if cooldown_elapsed and first_cycle_ok:
                    _recover_session(sid, s, stall_data)
                    _write_stall_marker(sid, stall_data)
        else:
            # Session is alive — clear stall marker if it existed, and
            # reset the per-session log bucket so a future suppression
            # starts fresh.
            _bucket_tracker.pop(sid, None)
            if stall_data is not None:
                attempts = stall_data.get("recovery_attempts", 0)
                if attempts > 0:
                    _log(f"STALL cleared sid={sid[:8]} — recovered after {attempts} attempt(s)")
                else:
                    _log(f"STALL cleared sid={sid[:8]} — transcript growing again")
                _clear_stall_marker(sid)


def _run_loop() -> None:
    """Main watchdog loop. Runs until killed."""
    recover_str = f"recover={'on' if AUTO_RECOVER else 'off'} max={RECOVER_MAX} cooldown={RECOVER_COOLDOWN_MIN}min"
    _log(f"started (interval={INTERVAL}s, stall={STALL_MIN}min, {recover_str}, pid={os.getpid()})")
    while True:
        try:
            _check_once()
        except Exception as e:
            _log(f"error in check: {e}")
        time.sleep(INTERVAL)


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_start() -> int:
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"Watchdog already running (pid {pid}).")
        return 0

    # Fork into background
    child = os.fork()
    if child > 0:
        # Parent
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(child))
        recover_str = "on" if AUTO_RECOVER else "off"
        print(f"Watchdog started (pid {child}). Checking every {INTERVAL}s, stall threshold {STALL_MIN}min, auto-recover {recover_str}.")
        return 0
    else:
        # Child — detach
        os.setsid()
        # Close stdio
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)

        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            _run_loop()
        finally:
            try:
                PID_FILE.unlink()
            except Exception:
                pass
        sys.exit(0)


def cmd_stop() -> int:
    pid = _read_pid()
    if not pid:
        print("Watchdog not running (no PID file).")
        return 0
    if not _is_running(pid):
        print(f"Watchdog not running (stale PID {pid}).")
        try:
            PID_FILE.unlink()
        except Exception:
            pass
        return 0
    os.kill(pid, signal.SIGTERM)
    # Wait briefly
    for _ in range(10):
        if not _is_running(pid):
            break
        time.sleep(0.2)
    try:
        PID_FILE.unlink()
    except Exception:
        pass
    print(f"Watchdog stopped (pid {pid}).")
    _log("stopped")
    return 0


def cmd_status() -> int:
    pid = _read_pid()
    if pid and _is_running(pid):
        recover_str = "on" if AUTO_RECOVER else "off"
        print(f"Watchdog running (pid {pid}). auto-recover={recover_str} max={RECOVER_MAX} cooldown={RECOVER_COOLDOWN_MIN}min")
        return 0
    else:
        print("Watchdog not running.")
        return 1


def cmd_run() -> int:
    """Foreground mode for debugging."""
    recover_str = "on" if AUTO_RECOVER else "off"
    print(f"Watchdog running in foreground (interval={INTERVAL}s, stall={STALL_MIN}min, auto-recover={recover_str})")
    print("Press Ctrl+C to stop.")
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    _run_loop()
    return 0


def cmd_recover(session_id: str) -> int:
    """Manually trigger recovery for a specific session."""
    sessions = _load_active_sessions()
    target = None
    for s in sessions:
        sid = s.get("session_id", "")
        if session_id in sid:
            target = s
            break
    if not target:
        print(f"No active session matching '{session_id}'.", file=sys.stderr)
        return 1

    sid = target["session_id"]
    stall_data = _read_stall_marker(sid) or {
        "detected_at": time.time(),
        "recovery_attempts": 0,
        "last_recovery_at": None,
        "unrecoverable": False,
        "recovered_pids": [],
    }
    if _recover_session(sid, target, stall_data):
        _write_stall_marker(sid, stall_data)
        print(f"Recovery spawned for {sid[:12]}… (attempt {stall_data['recovery_attempts']})")
        return 0
    else:
        print("Recovery failed.", file=sys.stderr)
        return 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        return cmd_start()
    elif cmd == "stop":
        return cmd_stop()
    elif cmd == "status":
        return cmd_status()
    elif cmd == "run":
        return cmd_run()
    elif cmd == "recover":
        if len(sys.argv) < 3:
            print("Usage: watchdog.py recover <session-id-prefix>", file=sys.stderr)
            return 1
        return cmd_recover(sys.argv[2])
    else:
        print(f"Usage: {sys.argv[0]} start|stop|status|run|recover <sid>")
        return 1


if __name__ == "__main__":
    sys.exit(main())
