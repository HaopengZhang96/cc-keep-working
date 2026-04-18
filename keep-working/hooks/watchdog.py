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
# Default OFF. In practice `claude --resume <sid> -p "..."` spawns a
# non-interactive subprocess that does NOT unstick a hung interactive
# session — the original process still owns the session file, and the
# new process either exits immediately or writes to a different session.
# Empirically, 39 recovery attempts across multiple stalled sessions never
# revived the transcript. We keep the code paths but default OFF so we
# don't spawn zombie claude processes.
AUTO_RECOVER = os.environ.get("WATCHDOG_AUTO_RECOVER", "0") not in ("0", "false", "no")
RECOVER_MAX = int(os.environ.get("WATCHDOG_RECOVER_MAX", "") or 3)
RECOVER_COOLDOWN_MIN = int(os.environ.get("WATCHDOG_RECOVER_COOLDOWN_MIN", "") or 3)


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


def _any_claude_process_alive() -> bool:
    """Check whether ANY `claude` CLI process is currently running.

    This is a best-effort liveness signal. If the transcript is idle but
    a claude process is still alive, the session is likely doing a long
    tool call (not a true stall). If NO claude processes exist, Claude
    Code crashed / was closed / API hung — worth notifying.

    We can't scope this to a specific session_id because pgrep doesn't
    show the session argument. False negatives (claude running in another
    unrelated window) just mean we skip one stall notification — OK.
    """
    try:
        r = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        # pgrep unavailable — fall back to "assume alive" to avoid false positives
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

def _recover_session(sid: str, state: dict, stall_data: dict) -> bool:
    """Attempt to resume a stalled session via `claude --resume`.
    Returns True if the process was spawned."""
    try:
        cmd = ["claude", "--resume", sid, "-p", "继续工作"]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        stall_data["recovery_attempts"] = stall_data.get("recovery_attempts", 0) + 1
        stall_data["last_recovery_at"] = time.time()
        pids = stall_data.get("recovered_pids", [])
        pids.append(proc.pid)
        stall_data["recovered_pids"] = pids

        attempt = stall_data["recovery_attempts"]
        task = (state.get("task", "") or "")[:40]
        _log(f"RECOVER attempt {attempt}/{RECOVER_MAX} sid={sid[:8]} pid={proc.pid} task={task}")
        _notify(
            "keep-working: auto-recovering",
            f"Session {sid[:8]} — attempt {attempt}/{RECOVER_MAX}",
        )
        return True
    except FileNotFoundError:
        _log(f"RECOVER FAILED sid={sid[:8]} — 'claude' not found in PATH")
        _notify("keep-working: recovery failed", "claude CLI not found in PATH")
        return False
    except Exception as e:
        _log(f"RECOVER FAILED sid={sid[:8]} — {e}")
        return False


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
            # Check if any claude process is still alive. If yes, this is
            # most likely a legitimate long tool call (big search, slow bash),
            # not a real stall. Skip notification.
            claude_alive = _any_claude_process_alive()

            if stall_data is None:
                # --- First detection: notify + write marker, wait for confirmation ---
                if claude_alive:
                    _log(f"STALL suppressed sid={sid[:8]} age={int(age_min)}min — claude process still alive (likely long tool call)")
                    continue
                task = (s.get("task", "") or "")[:60]
                remaining = max(0, int((deadline - now) / 60)) if deadline else -1
                rem_str = f"{remaining}min left" if remaining >= 0 else "no deadline"
                body = (
                    f"Session {sid[:8]} stalled — no activity for {int(age_min)} min, "
                    f"no claude process running. Task: {task}... ({rem_str})"
                )
                if AUTO_RECOVER:
                    body += " Auto-recovery in ~1 cycle."
                _notify("keep-working: session stalled!", body)
                _log(f"STALL detected sid={sid[:8]} age={int(age_min)}min (no claude proc) transcript={transcript}")
                _write_stall_marker(sid, {
                    "detected_at": now,
                    "recovery_attempts": 0,
                    "last_recovery_at": None,
                    "unrecoverable": False,
                    "recovered_pids": [],
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
            # Session is alive — clear stall marker if it existed
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
