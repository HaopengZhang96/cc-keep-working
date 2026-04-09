# keep-working architecture

This doc explains the non-obvious design decisions. Read it if you're
contributing to the hook or the CLI — the code has reasons for being the
way it is, and some of them are hard-won.

## The core problem

Claude Code fires a `Stop` hook whenever the assistant finishes responding.
If the hook exits with code 2, Claude Code reads stderr as a continuation
signal and makes Claude keep working. That's the whole mechanism. The skill
wraps it in a deadline + caps + UX.

The hard parts are all in the edges:
- binding state to a session without knowing the session_id up front
- detecting "Claude is genuinely done" so we don't loop forever
- scanning transcripts quickly when they grow to tens of MB
- keeping concurrent sessions isolated
- not crashing Claude Code when things go wrong

## Two subcommands, one script

The script has two entry points:
- `keep-working.py bind` — run from a PreToolUse hook on every tool call
- `keep-working.py stop` — run from the Stop hook

They share state but do very different jobs. Keeping them in one file
makes install a single `cp` instead of two.

## State files

```
~/.claude/
├── keep-working-pending.json              # skill writes, bind claims
├── keep-working-stop-request               # CLI/skill writes, bind honors
└── keep-working/
    ├── log.txt                             # bounded-size audit log
    └── <prefix>_<sha1-16>.json             # one per active session
```

**Why the pending-file dance?** The skill runs inside Claude's turn, but
only the hook knows the `session_id`. Solution:

1. Skill writes `keep-working-pending.json` (no session_id field).
2. Skill tells Claude to start working → Claude makes its first tool call.
3. PreToolUse hook fires with the session_id in the payload.
4. Bind hook atomic-renames the pending file to
   `keep-working/<hash-of-sid>.json`.

The window between (1) and (3) is measured in milliseconds. A TTL
(default 10 min, `KEEP_WORKING_PENDING_TTL_SEC`) plus stale-file cleanup
protects against the rare case where Claude writes pending and then dies
before making a tool call.

**Why hash the session_id?** Different raw session_ids could sanitize to
the same filename after path-traversal scrubbing. SHA1-16 (first 16 hex
chars) gives a collision-free unique suffix.

**Why per-session files and not a single global state?** Multi-session
isolation. Two Claude Code windows can both be in keep-working mode
simultaneously, each with its own deadline and task. A single global
state file would cross-contaminate: session A's Stop hook would see
session B's state and block A based on B's deadline.

## Stagnation detection: delta scan, tool_use count

The hook needs to answer "is Claude actually working?" so it can release
a session that has truly nothing left to do (instead of looping forever).

**Failed approach 1: sum `usage.input_tokens`.** Each Anthropic API call's
`input_tokens` includes the ENTIRE prior context. Summing across calls
double-counts everything. The v0.1 hook reported 1.15M tokens used after
2 turns in a fresh session — nonsense.

**Failed approach 2: file size growth.** The transcript is append-only,
so file size monotonically increases. Any growth = progress? No — Claude
Code writes an assistant message to the transcript on every turn, even
pure-text "I'm done" turns. File size grows unconditionally between
stops, which means "no growth" is unreachable. Stagnation would never
trigger.

**Failed approach 3: tool_use count via full scan.** Correct signal
(did Claude actually call any tools this turn?), but scanning a 50MB
transcript on every Stop is too slow — the hook adds seconds to every
stop, which is noticeable.

**Failed approach 4: tool_use count via sliding window.** Scan only the
last 5MB of the transcript. Fast, but the window slides as the
transcript grows, which can cause the total count to DECREASE between
stops (old tool_use blocks drop out of the scan). Decreasing count
looks like stagnation, but isn't.

**Current approach: tool_use count in delta scan.** Record the byte
offset of each scan in state. On the next stop, read from that offset to
the current file size — this is usually a few hundred bytes (one new
JSONL line). Count tool_use blocks in that delta only. This is:
- **Fast.** First scan on 14MB transcript: ~33ms (capped at 5MB from
  end). Subsequent delta scans: ~19ms (basically just Python startup).
- **Correct.** The delta captures exactly "what Claude did since the
  last stop". If the delta has 0 tool_use blocks, Claude did nothing
  actionable in that turn.
- **Robust.** The delta can't decrease (append-only file), so the
  signal is monotonic.

After 3 consecutive empty deltas (configurable via
`KEEP_WORKING_STAGNATION_CAP`), the hook releases the session. This
handles "Claude is legitimately done" without false positives from
slow or thinking turns.

Edge case: on the FIRST stop of a session, `prev_offset = 0` but
transcript is already large. We cap the scan at `SCAN_MAX_BYTES` from
the end and set `clean_boundary = False` so the partial first line
(after mid-line seek) gets dropped. On subsequent stops,
`prev_offset` is always a known line boundary from the previous
`curr_size`, so we DON'T drop the first line (doing so would lose real
new content — this was a bug caught during development).

## Token cap: max-per-message, not sum

As noted above, summing `usage.input_tokens` double-counts. The current
approximation: the MAX single-message `input + output + cache_creation +
cache_read`. This is a rough proxy for "current context window used",
not actual billable tokens. It's fine for the "prevent runaway" purpose
and wrong for anything accounting-related.

We also track the max across all deltas (not just the current one) so
the number doesn't suddenly shrink.

## Early stop: stop-request flag

The user says "停止持续工作". Claude needs to deactivate the session. But
Claude doesn't know its own `session_id`.

Solution: write `~/.claude/keep-working-stop-request` (any content) and
make any tool call. The next PreToolUse bind sees the flag, deletes
the current session's state file, and removes the flag.

The flag is scoped to the session that fires its PreToolUse hook next —
which is the session that wrote the flag, because Claude's tool call
immediately follows the write. If multiple sessions all have pending
stop-requests, the CLI's `stop` command can also write the flag; each
session's next bind will consume it.

## Hook safety contract

The `bind` subcommand **must never block a tool call**. Any internal
error — corrupt JSON, missing directory, permission denied — is caught
and logged, then `sys.exit(0)` is called. The tool proceeds normally.

The `stop` subcommand **fails open**: on internal error, it allows the
stop (exit 0). A failing hook that wedges Claude for hours is much
worse than a failing hook that lets the session stop early. Exit 2 is
only ever taken in the happy path where state is valid, caps are
unreached, and there's a reason to continue.

## Logging

All hook events log to `~/.claude/keep-working/log.txt` with a 1MB
rotation (not rotation really — just truncate to half size when the
file exceeds the cap). This is explicitly to avoid
[claude-code#16047](https://github.com/anthropics/claude-code/issues/16047)
where a hook log grew to 48GB and broke all hooks silently.

## Concurrency

**Bind race**: N sessions all see a pending file and try to claim it.
Solved via `os.rename(PENDING_FILE, stage_name)` where each process uses
a unique stage name (`pid + time_ns`). `os.rename` is atomic on POSIX:
exactly one process can successfully move the pending file to a new
name; the rest get `FileNotFoundError`. The winner then finishes parsing
and writes the per-session state file.

Stress tested with 10 trials × 20 concurrent binds: always exactly one
session wins.

**Stop race**: multiple Stop hooks for the same session should never
fire in parallel (Claude Code serializes per-session), and different
sessions have different state files, so there's no contention.

## CLI vs hook separation

The CLI (`bin/keep-working`) is a convenience wrapper. It reads and
writes the same state files the hook does, using the same paths. It's
for human use: `status`, `list`, `extend`, `stop`, `clean`, `log`,
`doctor`, `config`, `version`.

The CLI intentionally duplicates a few helpers from the hook
(`_session_filename`, `_load_sessions`). It's tempting to import the
hook as a library, but the hook is designed to be loaded once per
invocation with minimal imports for startup speed. Keeping them
separate is cleaner.

## Environment variables

Both the hook and the CLI honor:

| Var | Who | Default | Purpose |
|---|---|---|---|
| `CLAUDE_HOME` | both | `~/.claude` | Override the Claude config dir — useful for testing |
| `KEEP_WORKING_NUDGE_CAP` | hook | 500 | Max nudges before forced release (defense in depth) |
| `KEEP_WORKING_STAGNATION_CAP` | hook | 3 | Release after N consecutive empty Stops |
| `KEEP_WORKING_LOG_MAX_BYTES` | hook | 1_000_000 | Log file size cap before truncation |
| `KEEP_WORKING_ORPHAN_TTL_SEC` | hook | 86400 (24h) | Drop session files older than this |
| `KEEP_WORKING_PENDING_TTL_SEC` | hook | 600 (10min) | Drop pending files older than this |
| `KEEP_WORKING_SCAN_MAX_BYTES` | hook | 5_000_000 | Max bytes per scan window |

The hook only reads env vars at subprocess start, so changing them
doesn't affect a currently-running session's hook invocations — but
the NEXT invocation (on the next Stop) will pick up the new values.

## Things explicitly not implemented

- **Pause/resume.** A paused session would need "effective deadline"
  tracking across multiple pauses — a lot of state for a feature
  nobody has actually asked for.
- **Per-project configuration.** All config is global via env vars.
  Per-project would require resolving "which project" in the hook,
  which doesn't have reliable cwd context.
- **Network/Slack/Discord notifications.** Would add deps and failure
  modes. Users can wrap the CLI.
- **True cross-session locking.** See bind race above. The atomic-ish
  rename is good enough in practice.

## Things explicitly implemented with a thumb on the scale

- **Exit 0 on any unexpected error in both subcommands.** Better to
  fail open than to wedge Claude.
- **Hard ceiling on duration (24h), max_turns (1000), max_tokens (20M)**
  even when user asks for more. The skill clamps these before writing
  pending. The hook ALSO has an internal `NUDGE_CAP` (default 500,
  hard cap 5000) that runs separately from `max_turns`, as defense in
  depth.
- **Orphan cleanup on every bind.** O(N) over session files. If you
  have 10000 orphans, this gets slow, but in practice you'll have 0-2
  sessions at any time.
- **chmod 600 on state files.** Task descriptions can be sensitive.
- **Logs bounded to 1MB.** See above.
