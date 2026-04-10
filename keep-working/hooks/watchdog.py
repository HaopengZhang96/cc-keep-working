#!/usr/bin/env python3
"""
keep-working watchdog — background heartbeat monitor.

Detects when a keep-working session has stalled (Claude Code process
died, API timed out, network dropped) by monitoring transcript file
growth. If no growth for WATCHDOG_STALL_MIN minutes, fires a desktop
notification and optionally writes a stall marker.

Usage:
    python3 watchdog.py start           # daemonize, write PID file
    python3 watchdog.py stop            # kill the daemon
    python3 watchdog.py status          # check if running
    python3 watchdog.py run             # foreground (for debugging)

The watchdog checks every WATCHDOG_INTERVAL_SEC seconds (default 60).
If the transcript file hasn't grown in WATCHDOG_STALL_MIN minutes
(default 5), it considers the session stalled.

Env vars:
    WATCHDOG_INTERVAL_SEC       Check interval (default 60)
    WATCHDOG_STALL_MIN          Minutes without growth = stalled (default 5)
    CLAUDE_HOME                 Override ~/.claude
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
STALL_MIN = int(os.environ.get("WATCHDOG_STALL_MIN", "") or 5)


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
            # Also try transcript_path from the session's last stop
            continue

        try:
            mtime = os.path.getmtime(transcript)
        except OSError:
            continue

        age_min = (now - mtime) / 60
        stall_marker = SESSIONS_DIR / f".stall-{sid[:16]}"

        if age_min > STALL_MIN:
            if not stall_marker.exists():
                # First detection — notify
                task = (s.get("task", "") or "")[:60]
                remaining = max(0, int((deadline - now) / 60)) if deadline else -1
                rem_str = f"{remaining}min left" if remaining >= 0 else "no deadline"
                body = (
                    f"Session {sid[:8]} stalled — no activity for {int(age_min)} min. "
                    f"Task: {task}... ({rem_str})"
                )
                _notify("keep-working: session stalled!", body)
                _log(f"STALL detected sid={sid[:8]} age={int(age_min)}min transcript={transcript}")
                try:
                    stall_marker.write_text(str(now))
                except Exception:
                    pass
        else:
            # Session is alive — clear stall marker if it existed
            if stall_marker.exists():
                _log(f"STALL cleared sid={sid[:8]} — transcript growing again")
                try:
                    stall_marker.unlink()
                except Exception:
                    pass


def _run_loop() -> None:
    """Main watchdog loop. Runs until killed."""
    _log(f"started (interval={INTERVAL}s, stall={STALL_MIN}min, pid={os.getpid()})")
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
        print(f"Watchdog started (pid {child}). Checking every {INTERVAL}s, stall threshold {STALL_MIN}min.")
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
        print(f"Watchdog running (pid {pid}).")
        return 0
    else:
        print("Watchdog not running.")
        return 1


def cmd_run() -> int:
    """Foreground mode for debugging."""
    print(f"Watchdog running in foreground (interval={INTERVAL}s, stall={STALL_MIN}min)")
    print("Press Ctrl+C to stop.")
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    _run_loop()
    return 0


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
    else:
        print(f"Usage: {sys.argv[0]} start|stop|status|run")
        return 1


if __name__ == "__main__":
    sys.exit(main())
