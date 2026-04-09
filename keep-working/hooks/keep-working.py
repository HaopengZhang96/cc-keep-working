#!/usr/bin/env python3
"""
keep-working hook v2.

Subcommands (passed as argv[1]):

  bind  — run from a PreToolUse hook. On every tool call:
            1. If a stop-request flag exists, deactivate THIS session and clear it.
            2. If a pending state file exists and this session has none yet,
               atomically claim the pending file as ~/.claude/keep-working/<sid>.json.
            3. Sweep orphan session files (>24h old).
          Always exits 0 — never blocks tool execution.

  stop  — run from the Stop hook. Decides whether to allow Claude to stop:
            * stop_hook_active set                 → allow (recursion guard)
            * no state file for this session       → allow
            * deadline / turn cap / token cap hit  → allow + clear state
            * stagnation: N consecutive empty stops (no new tool calls) → allow + clear
            * nudge cap exceeded                   → allow + clear
            * otherwise                            → block (exit 2 + JSON on stderr)

State files live under ~/.claude/keep-working/ keyed by a hash of session_id,
so multiple concurrent keep-working sessions don't interfere with each other.

Tunable env vars (all optional):
  KEEP_WORKING_NUDGE_CAP        Max nudges per session (default 500, hard cap 5000)
  KEEP_WORKING_STAGNATION_CAP   Allow stop after N empty stops in a row (default 3)
  KEEP_WORKING_LOG_MAX_BYTES    Max log size before truncation (default 1_000_000)
  KEEP_WORKING_ORPHAN_TTL_SEC   Orphan session-file TTL (default 86400 = 24h)
  KEEP_WORKING_PENDING_TTL_SEC  Pending file TTL (default 600 = 10 min)
  KEEP_WORKING_SCAN_MAX_BYTES   Max bytes to scan from transcript (default 5_000_000)

Compatible with Claude Code Stop / PreToolUse hook protocol:
  - Reads JSON payload from stdin (session_id, transcript_path, stop_hook_active)
  - exit 2 + JSON on stderr blocks the stop and feeds reason back as a user turn
"""
from __future__ import annotations

__version__ = "0.2.0"

import hashlib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

# ---------- paths & config ----------

CLAUDE_DIR = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
SESSIONS_DIR = CLAUDE_DIR / "keep-working"
PENDING_FILE = CLAUDE_DIR / "keep-working-pending.json"
STOP_REQUEST_FILE = CLAUDE_DIR / "keep-working-stop-request"
LOG_FILE = SESSIONS_DIR / "log.txt"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


PENDING_TTL_SEC = _env_int("KEEP_WORKING_PENDING_TTL_SEC", 600)
ORPHAN_TTL_SEC = _env_int("KEEP_WORKING_ORPHAN_TTL_SEC", 86_400)
NUDGE_CAP_DEFAULT = min(_env_int("KEEP_WORKING_NUDGE_CAP", 500), 5_000)
STAGNATION_CAP = max(1, _env_int("KEEP_WORKING_STAGNATION_CAP", 3))
LOG_MAX_BYTES = max(10_000, _env_int("KEEP_WORKING_LOG_MAX_BYTES", 1_000_000))
# Defense in depth: ignore deadlines farther than this in the future.
# SKILL.md also clamps hours ≤ 24 on the Claude side, but a buggy / hand-
# crafted pending file could set deadline_epoch = now + 100 years.
MAX_DEADLINE_HORIZON_SEC = _env_int("KEEP_WORKING_MAX_HORIZON_SEC", 25 * 3600)


# ---------- low-level helpers ----------

def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _session_filename(sid: str) -> Path:
    """Hash session_id so different ids never collide after sanitization."""
    h = hashlib.sha1(sid.encode("utf-8", errors="replace")).hexdigest()[:16]
    safe_prefix = re.sub(r"[^A-Za-z0-9_\-]", "_", sid)[:32]
    return SESSIONS_DIR / f"{safe_prefix}_{h}.json"


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def _write_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, indent=2))
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def _log(msg: str) -> None:
    """Append a line to the log, truncating if it exceeds size cap."""
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        # Rotate (truncate) if too large.
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                tail = LOG_FILE.read_text(errors="replace")[-(LOG_MAX_BYTES // 2):]
                LOG_FILE.write_text(tail)
        except Exception:
            pass
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


# ---------- transcript inspection ----------

SCAN_MAX_BYTES = _env_int("KEEP_WORKING_SCAN_MAX_BYTES", 5_000_000)  # 5MB


def _iter_transcript_lines(transcript_path: str):
    """
    Yield JSONL lines from transcript, reading only the last SCAN_MAX_BYTES
    if the file is larger. This bounds Stop-hook latency on long sessions
    where the transcript can grow to tens of MB.
    """
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return
    try:
        with open(transcript_path, "rb") as f:
            if size > SCAN_MAX_BYTES:
                f.seek(size - SCAN_MAX_BYTES)
                # Drop the (likely partial) first line after the seek.
                f.readline()
            for raw in f:
                try:
                    yield raw.decode("utf-8", errors="replace")
                except Exception:
                    continue
    except Exception:
        return


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path) if path and os.path.exists(path) else 0
    except OSError:
        return 0


def _scan_transcript_delta(transcript_path: str, start_offset: int) -> tuple[int, int]:
    """
    Incrementally scan the transcript from byte `start_offset` to end.
    Returns (new_tool_use_count_in_delta, max_context_tokens_in_delta).

    Incremental scanning is how we get reliable stagnation detection:
    - Between two consecutive Stop hook calls, the assistant always writes
      at least one new line to the transcript. So file-size growth is a
      useless "progress" signal — it's always true.
    - What we actually want to know is: "did Claude make any tool calls in
      the turn that just ended?" Counting tool_use blocks in the NEW
      portion of the transcript (between the last scan and now) answers
      exactly that.

    Also bounds work per invocation. If the file hasn't grown, this is a
    zero-byte read. If it has grown by 2KB, we scan 2KB. Even on long
    sessions where total transcript is >100MB, per-Stop latency stays
    bounded by the size of the last turn.

    Edge cases:
    - If start_offset is beyond the current file size (transcript shrank
      or rotated — should never happen for append-only JSONL but handle
      defensively), rescan from 0 up to SCAN_MAX_BYTES.
    - Partial last line (turn still being written) — we drop it.
    """
    curr_size = _file_size(transcript_path)
    if curr_size == 0:
        return 0, 0
    clean_boundary = True  # True when start_offset is at a known line boundary
    if start_offset > curr_size or start_offset < 0:
        start_offset = max(0, curr_size - SCAN_MAX_BYTES)
        clean_boundary = False
    # Cap delta size at SCAN_MAX_BYTES to prevent pathological scans.
    if curr_size - start_offset > SCAN_MAX_BYTES:
        start_offset = curr_size - SCAN_MAX_BYTES
        clean_boundary = False

    tool_uses = 0
    max_tok = 0
    try:
        with open(transcript_path, "rb") as f:
            f.seek(start_offset)
            # Only drop the first line when our seek landed mid-line
            # (SCAN_MAX_BYTES cap). On a clean incremental scan where
            # start_offset is the file size at a previous stop, it's
            # already a line boundary.
            if not clean_boundary and start_offset > 0:
                f.readline()
            for raw in f:
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                stack = [obj]
                while stack:
                    cur = stack.pop()
                    if isinstance(cur, dict):
                        if cur.get("type") == "tool_use":
                            tool_uses += 1
                        u = cur.get("usage")
                        if isinstance(u, dict):
                            it = u.get("input_tokens", 0) or 0
                            ot = u.get("output_tokens", 0) or 0
                            cc = u.get("cache_creation_input_tokens", 0) or 0
                            cr = u.get("cache_read_input_tokens", 0) or 0
                            if isinstance(it, (int, float)):
                                total = int(it) + int(ot) + int(cc) + int(cr)
                                if total > max_tok:
                                    max_tok = total
                        for v in cur.values():
                            if isinstance(v, (dict, list)):
                                stack.append(v)
                    elif isinstance(cur, list):
                        stack.extend(cur)
    except Exception:
        pass
    return tool_uses, max_tok


# ---------- subcommand: bind (PreToolUse) ----------

def cmd_bind(payload: dict) -> None:
    """Always exits 0. Never blocks a tool call."""
    try:
        sid = payload.get("session_id")
        if not sid:
            return
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            return

        sf = _session_filename(sid)

        # 1. Process stop-request flag for this session.
        if STOP_REQUEST_FILE.exists():
            if sf.exists():
                _safe_unlink(sf)
                _log(f"bind: stop-request honored for sid={sid[:8]}")
            _safe_unlink(STOP_REQUEST_FILE)

        # 2. Sweep orphan session files (older than ORPHAN_TTL_SEC).
        try:
            now = time.time()
            for child in SESSIONS_DIR.iterdir():
                if child.name == LOG_FILE.name:
                    continue
                if not child.name.endswith(".json"):
                    continue
                try:
                    if now - child.stat().st_mtime > ORPHAN_TTL_SEC:
                        _safe_unlink(child)
                        _log(f"bind: swept orphan {child.name}")
                except Exception:
                    pass
        except Exception:
            pass

        # 3. Claim pending file → bind to this session.
        if not PENDING_FILE.exists():
            return
        if sf.exists():
            # Don't clobber existing state for this session.
            return

        # Atomic claim: rename the pending file to a unique staging name.
        # At most one process can succeed; the rest get FileNotFoundError.
        # This eliminates the multi-claimer race when N sessions all see
        # the pending file and all try to read/write/unlink in parallel.
        stage = PENDING_FILE.with_name(
            f".keep-working-pending-stage-{os.getpid()}-{time.time_ns()}"
        )
        try:
            os.rename(PENDING_FILE, stage)
        except FileNotFoundError:
            return  # another session won the race
        except Exception:
            return

        # We exclusively own `stage` now. Parse, stamp, and finalize.
        try:
            data = json.loads(stage.read_text())
        except Exception:
            _safe_unlink(stage)
            return

        created = float(data.get("created_at_epoch", 0) or 0)
        if created and (time.time() - created) > PENDING_TTL_SEC:
            _safe_unlink(stage)
            _log(f"bind: dropped stale pending (age={int(time.time()-created)}s)")
            return

        # Clamp an insane deadline at claim time.
        now = time.time()
        requested = float(data.get("deadline_epoch", 0) or 0)
        if requested > now + MAX_DEADLINE_HORIZON_SEC:
            data["deadline_epoch"] = now + MAX_DEADLINE_HORIZON_SEC
            _log(
                f"bind: clamped deadline from {requested} → "
                f"{data['deadline_epoch']} (horizon {MAX_DEADLINE_HORIZON_SEC}s)"
            )

        data["session_id"] = sid
        data["bound_at_epoch"] = now
        data.setdefault("nudge_count", 0)
        data.setdefault("empty_stops", 0)
        data.setdefault("last_transcript_size", 0)
        _write_state(sf, data)
        _safe_unlink(stage)
        _log(f"bind: claimed pending for sid={sid[:8]} task={(data.get('task') or '')[:40]!r}")
    except Exception:
        _log(f"bind: unhandled error: {traceback.format_exc().splitlines()[-1]}")
    finally:
        sys.exit(0)


# ---------- subcommand: stop ----------

def _emit_block(reason: str) -> None:
    """Block the stop by exiting 2. Claude Code reads stderr and feeds it
    back as a continuation signal. We output plain text because that's
    what Claude Code's current Stop-hook path actually renders to the user
    (verified empirically). A pure-JSON output (per issue #10412) would
    produce ugly double-display since Claude Code surfaces the whole
    stderr. When the JSON path stabilizes in Claude Code we can switch.
    """
    try:
        sys.stderr.write(reason + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    sys.exit(2)


def _detect_chinese(s: str) -> bool:
    """True if the string contains CJK characters — used to pick the language
    of the continuation message shown to Claude."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in s or "")


def _build_reason(task: str, remaining_min: int, nudge: int, cap: int,
                  ctx_tokens: int, max_tokens: int, empty_stops: int) -> str:
    rem = f"~{remaining_min} min" if remaining_min >= 0 else "no deadline"
    cap_str = f"{cap}" if cap else "∞"
    tok_str = f"{ctx_tokens}/{max_tokens}" if max_tokens else f"{ctx_tokens}"
    if _detect_chinese(task):
        return (
            f"[keep-working] 不要停止。请继续执行：{task}\n"
            f"剩余：{rem}。Nudge {nudge}/{cap_str}。"
            f"上下文 tokens：{tok_str}。空停：{empty_stops}/{STAGNATION_CAP}。\n"
            "如果当前子任务完成了，请立即挑选下一个对目标最有价值的子任务继续工作 — "
            "不要只做总结。如果真的需要用户输入，不要问：挑最合理的默认值继续，"
            "用户之后可以纠正。如果真的没事可做了，不要调用任何工具直接停止，"
            f"停滞检测器会在连续 {STAGNATION_CAP} 次空停后释放你。"
        )
    else:
        return (
            f"[keep-working] DO NOT STOP. Continue working on: {task}\n"
            f"Time remaining: {rem}. Nudge {nudge}/{cap_str}. "
            f"Context tokens: {tok_str}. Empty stops: {empty_stops}/{STAGNATION_CAP}.\n"
            "If the current sub-task is finished, pick the NEXT most useful "
            "sub-task toward the goal and keep working — do not just summarize. "
            "If you genuinely need user input, do not ask: pick the most "
            "reasonable default and proceed. If there is truly nothing left "
            "to do, stop without making any tool calls and the stagnation "
            f"detector will release you after {STAGNATION_CAP} empty stops."
        )


def cmd_stop(payload: dict) -> None:
    try:
        # Recursion guard.
        if payload.get("stop_hook_active"):
            sys.exit(0)

        sid = payload.get("session_id")
        if not sid:
            sys.exit(0)
        sf = _session_filename(sid)
        if not sf.exists():
            sys.exit(0)
        try:
            state = json.loads(sf.read_text())
        except Exception:
            _log(f"stop: corrupt state file for sid={sid[:8]}, removing")
            _safe_unlink(sf)
            sys.exit(0)
        if not state.get("active"):
            _safe_unlink(sf)
            sys.exit(0)

        now = time.time()
        deadline = float(state.get("deadline_epoch", 0) or 0)
        max_turns = int(state.get("max_turns", 0) or 0)
        max_tokens = int(state.get("max_tokens", 0) or 0)
        nudge_cap = int(state.get("nudge_cap", NUDGE_CAP_DEFAULT) or NUDGE_CAP_DEFAULT)
        nudge_count = int(state.get("nudge_count", 0) or 0) + 1
        task = state.get("task", "the assigned task")

        # Time cap.
        if deadline and now >= deadline:
            _safe_unlink(sf)
            _log(f"stop: deadline reached sid={sid[:8]}")
            sys.exit(0)

        # Nudge / turn cap (defense in depth).
        if max_turns and nudge_count > max_turns:
            _safe_unlink(sf)
            _log(f"stop: turn cap {max_turns} exceeded sid={sid[:8]}")
            sys.exit(0)
        if nudge_count > nudge_cap:
            _safe_unlink(sf)
            _log(f"stop: nudge cap {nudge_cap} exceeded sid={sid[:8]}")
            sys.exit(0)

        # Incremental transcript scan: from the byte offset we saved on
        # the previous stop, read only NEW content. This is (a) fast even
        # on multi-MB transcripts, (b) gives us a true "did Claude do
        # anything in the last turn" signal for stagnation detection,
        # (c) gives us the max per-turn token usage for the token cap.
        transcript_path = payload.get("transcript_path", "")
        prev_offset = int(state.get("last_transcript_size", 0) or 0)
        delta_tools, delta_max_tok = _scan_transcript_delta(
            transcript_path, prev_offset
        )
        curr_size = _file_size(transcript_path)
        ctx_tokens = max(
            int(state.get("last_ctx_tokens", 0) or 0),
            delta_max_tok,
        )

        if max_tokens and ctx_tokens >= max_tokens:
            _safe_unlink(sf)
            _log(f"stop: token cap {max_tokens} exceeded (ctx={ctx_tokens}) sid={sid[:8]}")
            sys.exit(0)

        # Stagnation: no new tool_use blocks in the delta since last stop.
        empty_stops = int(state.get("empty_stops", 0) or 0)
        if delta_tools == 0:
            empty_stops += 1
        else:
            empty_stops = 0
        if empty_stops >= STAGNATION_CAP:
            _safe_unlink(sf)
            _log(
                f"stop: stagnation ({empty_stops} empty stops, no tool calls) "
                f"sid={sid[:8]} — releasing"
            )
            sys.exit(0)

        # Persist counters.
        state["nudge_count"] = nudge_count
        state["last_transcript_size"] = curr_size
        state["last_ctx_tokens"] = ctx_tokens
        state["empty_stops"] = empty_stops
        state["last_stop_epoch"] = now
        _write_state(sf, state)

        remaining_min = max(0, int((deadline - now) // 60)) if deadline else -1
        reason = _build_reason(
            task, remaining_min, nudge_count, max_turns,
            ctx_tokens, max_tokens, empty_stops,
        )
        _log(
            f"stop: blocked sid={sid[:8]} nudge={nudge_count} "
            f"empty={empty_stops} ctx={ctx_tokens}"
        )
        _emit_block(reason)
    except SystemExit:
        raise
    except Exception:
        _log(f"stop: unhandled error: {traceback.format_exc().splitlines()[-1]}")
        sys.exit(0)


# ---------- entrypoint ----------

def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stop"
    if cmd in ("-V", "--version", "version"):
        print(f"keep-working hook {__version__}")
        sys.exit(0)
    payload = _read_payload()
    if cmd == "bind":
        cmd_bind(payload)
    elif cmd == "stop":
        cmd_stop(payload)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
