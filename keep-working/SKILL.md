---
name: keep-working
description: Force Claude Code to work continuously for a specified duration without stopping early. Triggers when the user asks Claude to "keep working" / "work continuously" / "持续工作" / "连续工作" / "不要停" and specifies a duration (hours OR minutes). Supports hard caps on turns and tokens, autonomous mode, multi-session isolation, and clean early termination.
---

# keep-working / 持续工作

Lock Claude Code into continuous-work mode for a fixed wall-clock duration.
The skill writes a session-state file; a `Stop` hook reads it on every stop
attempt and either blocks the stop (injecting "keep going") or releases it
(deadline / cap / stagnation hit). A `PreToolUse` hook claims the state
file for the current session on its first tool call.

## When to invoke

Trigger when the user message matches BOTH of:

1. A "keep working" intent in any of:
   - 中文：`持续工作`、`连续工作`、`不要停`、`不停地工作`、`一直做`
   - English: `keep working`, `work continuously`, `don't stop`, `nonstop`,
     `work for N hours`
2. A duration specification (hours OR minutes), e.g.:
   - `3 小时`、`1.5 小时`、`30 分钟`、`90 分钟`、`半小时`
   - `3 hours`, `2h`, `30 min`, `90m`, `1.5h`

If the user mentions "keep working" but provides no duration, ask ONCE in a
single short sentence what duration they want — unless they also say
"全程不用问我" / "don't ask me" / "autonomous", in which case default to
1 hour and tell them.

## Parameters to extract

| Param | Required | Default | Hard ceiling | Notes |
|---|---|---|---|---|
| `duration` | yes | — | 24 hours | Accept hours OR minutes. Convert minutes → hours / 60. |
| `task` | yes | — | — | What to work on. If absent and user is in autonomous mode, infer from prior conversation. |
| `max_turns` | no | 200 | 1000 | Hook also defends with `KEEP_WORKING_NUDGE_CAP` env var. |
| `max_tokens` | no | 2,000,000 | 20,000,000 | Approximate context-tokens cap (max single message, not sum). |

Refuse silently-and-cap if user asks for >24h, >1000 turns, or >20M tokens —
clamp to the ceiling and tell them in one line.

## What to do when invoked

### 1. Verify the hook is installed

Read `~/.claude/settings.json`. There must be TWO hooks both pointing at
`~/.claude/hooks/keep-working.py`:

- a `PreToolUse` hook with arg `bind`
- a `Stop` hook with arg `stop`

If either is missing, install both (see "Installation" below) and tell the
user one line that you set them up. Use Read + Edit to merge — never
overwrite existing hooks the user may already have.

### 2. Write the pending state file

Use the Write tool to create `~/.claude/keep-working-pending.json`:

```json
{
  "active": true,
  "deadline_epoch": <now + duration_hours * 3600>,
  "max_turns": <max_turns>,
  "max_tokens": <max_tokens>,
  "nudge_count": 0,
  "empty_stops": 0,
  "last_transcript_size": 0,
  "task": "<task description, ≤500 chars>",
  "started_at": "<ISO 8601 with offset>",
  "created_at_epoch": <now>
}
```

The very next tool call you make will fire the `PreToolUse` bind hook,
which atomically renames this pending file into
`~/.claude/keep-working/<hashed_session_id>.json`. **Do not** write directly
into `~/.claude/keep-working/` — let the bind hook do it, that's how
multi-session isolation works.

### 3. Confirm in one short message

Examples:

> 已启动持续工作模式：连续 3 小时（截止北京时间 15:42），上限 200 轮 / 2M tokens。说"停止持续工作"可提前结束。

> Keep-working active: 1 hour (until 15:42 local), caps 200 nudges / 2M tokens. Say "stop keep working" to end early.

Include the **wall-clock end time in the user's local timezone** (or Beijing
time if the user has been speaking Chinese).

### 4. Begin the task immediately

Do not wait for further input. Make the first real tool call right away — it
will trigger bind. Then drive the task to completion.

## Behavior during keep-working mode

- After each natural stop, the hook will inject a "DO NOT STOP. Continue
  working on: <task>" message. Treat that as a new user turn and pick the
  next most useful sub-task.
- **Do not ask the user questions during keep-working mode.** If you would
  normally ask, instead pick the most reasonable default and proceed —
  document the assumption in your work and the user can correct later.
- If you have truly exhausted all useful sub-tasks, just stop without making
  any tool calls. The hook's stagnation detector releases the session after
  3 consecutive empty stops.
- Use `TodoWrite` to track your own progress across the long run — it
  prevents you from losing track of where you are.
- Periodically (every 15-30 minutes of work) write a brief progress note to
  the user as a regular text response. This is the only place they can see
  what you're doing.

## How to stop early

If the user says any of:

- 中文：`停止持续工作`、`结束持续工作`、`取消持续工作`
- English: `stop keep working`, `cancel keep working`, `end keep-working`

Do this:

1. Use Write to create the file `~/.claude/keep-working-stop-request` with
   any content (`"stop"` is fine).
2. Make any tool call (e.g. `Bash echo done`) so the next `PreToolUse` bind
   hook fires.
3. The bind hook will deactivate the current session's state file and
   delete the stop-request flag.
4. Confirm to the user in one line.

This avoids the need for Claude to know its own `session_id`.

## Reporting completion

When the deadline is reached and the hook releases the stop, before
actually stopping write a brief summary to the user:

- What you accomplished
- Anything left over / blocked
- Wall-clock time you ran (in the user's timezone — Beijing time if Chinese)

## Installation (run once per machine)

If `~/.claude/hooks/keep-working.py` does not exist:

```bash
mkdir -p ~/.claude/hooks
cp <skill_dir>/hooks/keep-working.py ~/.claude/hooks/keep-working.py
chmod +x ~/.claude/hooks/keep-working.py
```

Then merge into `~/.claude/settings.json`. The two hooks must both point at
the SAME script with different arg:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/keep-working.py bind" }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/keep-working.py stop" }
        ]
      }
    ]
  }
}
```

Use Read + Edit to merge carefully — preserve any existing hooks the user
already has (do NOT clobber the entire `hooks` block).

There is also an `install.sh` in this skill's directory that does the above
idempotently if the user prefers shell.

## Safety: check the task before committing to N hours

Before writing the pending state file, briefly assess the task:

- **Destructive or irreversible actions** (`rm -rf`, `git push --force`,
  DROP TABLE, deleting production resources, mass-renaming files) —
  ask the user once to confirm, even if they said "autonomous". The
  user may have meant "keep refactoring" not "keep deleting". An extra
  30 seconds of confirmation beats 2 hours of damage.
- **Network calls to external services** — fine for reads (GET), but
  for writes (POST/PUT/DELETE) or API calls that cost money, confirm.
- **Modifying files outside the current project root** — confirm.
- **Anything touching `~/.claude/`, `~/.ssh/`, `~/.aws/`** — refuse
  and ask the user to do it themselves.

If the task is clearly safe and scoped (bug fixing, test writing,
refactoring, research, documentation, debugging), skip the
confirmation and proceed immediately.

## Worked example (what a session looks like)

User says: `请持续工作 30 分钟，把 tests/ 下所有测试跑一遍并修好红的`

1. You parse: `duration=0.5h`, `task="把 tests/ 下所有测试跑一遍并修好红的"`
2. You verify hooks are installed in `~/.claude/settings.json`, both
   present, done. (If not present, install them, tell user.)
3. You Write `~/.claude/keep-working-pending.json` with
   `deadline_epoch = now + 1800`.
4. You reply: "已启动持续工作模式：30 分钟（截止 14:12 北京时间），
   上限 200 轮 / 2M tokens。说「停止持续工作」可提前结束。"
5. You IMMEDIATELY run `pytest tests/ -v` (this first tool call fires
   PreToolUse bind; the hook atomically claims the pending file).
6. You see 3 failing tests. Fix the first. Run pytest again. Fix next. ...
7. After each natural stopping point, the Stop hook injects "DO NOT STOP.
   Continue working on: …". You pick the next useful sub-task.
8. At 14:12, the Stop hook sees the deadline, releases, and lets you
   stop. Before stopping, you write a brief summary to the user: "在
   30 分钟内跑了 X 次 pytest，修好了 N/3 个红 — 其中 Y 个是 A 问题，
   Z 个是 B 问题。剩余那个是…"

## FAQ

**Q: User asks "持续工作 2 小时" but gives no task description.**
A: If they also said "自治" / "全程不用问我" / "不用问", infer the task
from the conversation (e.g. the last topic you were working on) and
proceed. Otherwise ask ONE short question: "这 2 小时想让我做什么？"

**Q: User says "持续工作 50 小时".**
A: Clamp to 24h and tell them: "硬上限是 24 小时。我会连续工作 24 小时。"

**Q: What if Claude Code's Stop hook doesn't fire?**
A: Tell the user to run `~/.claude/skills/keep-working/bin/keep-working doctor`
to diagnose. Common causes: (a) skill installed via plugin system
(issue #10412 — install as raw skill), (b) `~/.claude/hooks.log` grew
to GB size (issue #16047 — delete it), (c) `settings.json` doesn't
have `hooks.Stop` registered.

**Q: Can the user extend an active session by 10 more minutes?**
A: Yes: `keep-working extend 10` (CLI). Or Claude can write directly to
the session file's `deadline_epoch` — but the CLI is cleaner.

**Q: Multi-session: two Claude Code windows both in keep-working mode.**
A: Each has its own state file keyed by session_id hash; they don't
interfere. `keep-working list` shows both.

## Caveats to mention if relevant

- This skill only works while the Claude Code process is alive. Closing the
  terminal kills the timer — there is no background daemon.
- Token counts are approximate (max-per-message, not exact billing).
- If installed via the plugin system, exit-code-2 Stop hooks may be silently
  ignored (Claude Code bug #10412). Always install via `~/.claude/hooks/`
  and `~/.claude/settings.json`, never via a plugin manifest.
