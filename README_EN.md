# cc-keep-working

A Claude Code skill that forces Claude to keep working for a fixed wall-clock duration instead of stopping early. Multi-session isolation, stagnation detection, incremental transcript scanning, configurable caps, and a full CLI toolkit.

## What it solves

Even with every permission granted and a prompt screaming "WORK FOR 10 HOURS", Claude Code routinely calls it quits after 30 minutes — the model decides the task is "done enough" and stops. This skill installs a `Stop` hook that catches that moment, injects a "keep going" message, and only lets the stop through when the wall-clock deadline, a hard cap, or the stagnation detector says it's actually done.

## Features

- ✅ Specify hours OR minutes (`3 hours` / `30 min` / `1.5h` / `90m`)
- ✅ Hard caps on turns and tokens, configurable, with hard ceilings
- ✅ Multi-session isolation — run N concurrently, no interference
- ✅ Stagnation detection — releases after 3 consecutive empty stops (no tool calls)
- ✅ Incremental delta transcript scan — ~20ms per stop, even on 100MB+ transcripts
- ✅ Atomic bind via `os.rename` — no race conditions with concurrent sessions
- ✅ Bilingual continuation messages — Chinese if task is Chinese, English otherwise
- ✅ Clean early stop via stop-request flag (no need to know session_id)
- ✅ Plain-text stderr injection (empirically compatible with current Claude Code)
- ✅ Log size capped (1MB) — won't repeat the [48GB disk-fill bug](https://github.com/anthropics/claude-code/issues/16047)
- ✅ CLI with 9 subcommands: `status --json` / `list` / `stop --force` / `extend ±N -s SID` / `config` / `doctor -q` / `clean` / `log -n N` / `version`
- ✅ Idempotent install / uninstall scripts that preserve existing hooks
- ✅ 61-test unittest suite with sandboxed `$HOME` / `CLAUDE_HOME`
- ✅ CI on ubuntu + macOS × Python 3.9–3.12
- ✅ session_id hashed (SHA1) → safe against path traversal
- ✅ State files chmod 600 (task descriptions may be sensitive)
- ✅ Deadline horizon clamp (25h max, defense in depth against insane deadlines)

## How it works

```
You: keep working for 3 hours on the auth refactor
       │
       ▼
Skill writes ~/.claude/keep-working-pending.json
       │
       ▼
Claude makes its first tool call ──► PreToolUse hook (bind)
                                            │
                                            ▼
                              atomic rename to
                              ~/.claude/keep-working/<sid_hash>.json
                                            │
                                            ▼
Claude works ──► tries to stop ──► Stop hook
                                       │
                       ┌───────────────┼─────────────┬──────────────┐
                       ▼               ▼             ▼              ▼
                   before              cap hit       3 empty       stop_hook_
                   deadline &          (time/turn/   stops in a    active
                   under caps          token)        row           (recursion)
                       │                  │            │              │
                   exit 2 +            exit 0       exit 0         exit 0
                   stderr              clear        clear
                   "keep going"
                       │
                       └──► Claude Code feeds stderr back as a user turn
                            → work resumes
```

**Concurrent sessions**: each session has its own state file under `~/.claude/keep-working/`, keyed by SHA1 hash of `session_id`. Run N keep-working sessions in parallel without interference.

## Install

**Recommended:**

```bash
git clone https://github.com/HaopengZhang96/cc-keep-working.git
cd cc-keep-working
bash keep-working/install.sh
```

The installer:
1. Copies the skill to `~/.claude/skills/keep-working/`
2. Copies the hook to `~/.claude/hooks/keep-working.py`
3. Idempotently merges PreToolUse + Stop hooks into `~/.claude/settings.json` (preserves your existing hooks)
4. Copies CLI helper and example scripts
5. Runs `doctor` to verify the setup

Re-running is safe. Use `keep-working/uninstall.sh` to remove cleanly.

**Manual install:**

```bash
mkdir -p ~/.claude/skills/keep-working ~/.claude/hooks
cp -r keep-working/* ~/.claude/skills/keep-working/
cp keep-working/hooks/keep-working.py ~/.claude/hooks/keep-working.py
chmod +x ~/.claude/hooks/keep-working.py
```

Then merge into `~/.claude/settings.json` (**preserve any existing hooks**):

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

> ⚠️ **Do not install as a plugin.** Claude Code [issue #10412](https://github.com/anthropics/claude-code/issues/10412) shows that plugin-installed Stop hooks have their exit code 2 ignored. Always install to `~/.claude/hooks/` directly.

## Usage

**Trigger phrases** (must include a duration):

- `keep working for 3 hours on the refactor`
- `work continuously for 90 min on X`
- `nonstop, 1.5h`
- `keep working 5 hours, max 300 turns 5M tokens`
- Chinese triggers also supported: `请持续工作 3 小时，重构 auth`

**Stop early:**

- `stop keep working` / `cancel keep working` / `end keep-working`
- `停止持续工作` / `结束持续工作`

## Performance

The hook runs on every Stop/PreToolUse — it must be fast. Benchmarks:

| Scenario | Latency |
|---|---|
| Empty transcript stop | ~10ms |
| 14MB transcript first stop (5MB cap-scan) | ~33ms |
| 14MB transcript subsequent stop (delta ~200B) | ~19ms |
| PreToolUse bind (no pending) | ~10ms |
| PreToolUse bind (claim pending, atomic rename) | ~15ms |
| 200 sequential stops total | ~3.8s (avg 19ms) |
| 20 concurrent binds | exactly 1 winner, rest no-op |

Incremental scanning: the hook saves the byte offset from each scan in the state file. Subsequent stops read only the new bytes. Even on 100MB+ transcripts, per-stop latency stays around 20ms.

Run your own benchmark: `bash keep-working/examples/benchmark.sh`

## CLI

`bin/keep-working` is a standalone CLI (doesn't require Claude Code to be running):

```bash
keep-working status              # detailed view of all active sessions
keep-working status --json       # machine-readable JSON output for scripting
keep-working list                # terse one-liner per session
keep-working stop                # write stop-request flag
keep-working extend 30           # extend all active sessions by 30 minutes
keep-working extend 30 -s abc    # only extend sessions matching "abc"
keep-working extend -15          # shorten by 15 minutes (negative)
keep-working config              # show env var configuration
keep-working doctor              # sanity-check the installation
keep-working doctor -q           # quiet mode, exit code only (for CI)
keep-working clean               # nuke all state files (emergency reset)
keep-working log -n 100          # tail hook log
keep-working version
```

Add to `$PATH`: `ln -s ~/.claude/skills/keep-working/bin/keep-working ~/bin/keep-working`

## Caps

| What | Default | Hard ceiling | Env override |
|---|---|---|---|
| `hours` | required | 24 | — |
| `max_turns` | 200 | 1000 | — |
| `max_tokens` | 2,000,000 | 20,000,000 | — |
| nudge_count (defense in depth) | 500 | 5000 | `KEEP_WORKING_NUDGE_CAP` |
| stagnation threshold | 3 empty stops | — | `KEEP_WORKING_STAGNATION_CAP` |
| orphan TTL | 24h | — | `KEEP_WORKING_ORPHAN_TTL_SEC` |
| pending TTL | 10 min | — | `KEEP_WORKING_PENDING_TTL_SEC` |
| log size | 1MB | — | `KEEP_WORKING_LOG_MAX_BYTES` |
| scan window | 5MB | — | `KEEP_WORKING_SCAN_MAX_BYTES` |
| Claude config dir | `~/.claude` | — | `CLAUDE_HOME` |
| deadline horizon | 25h | — | `KEEP_WORKING_MAX_HORIZON_SEC` |

Any cap hit → immediate release + state cleared.

## Token counting

We take **MAX over messages**, not SUM. Each Anthropic API call's `input_tokens` already includes the entire prior context, so summing them double-counts massively (this was a v0.1 bug). Now we use the largest single message's `input + output + cache_creation + cache_read` as the "current context size" approximation.

It's approximate. Don't bill against it.

## Stagnation detection

If Claude calls Stop 3 times in a row without making any tool calls in between (detected via incremental delta scan of the transcript), the hook concludes there's genuinely nothing left to do and releases the session. This prevents the "task is finished but the hook keeps stuffing 'keep going' down its throat" failure mode.

The SKILL.md also tells Claude: in keep-working mode, don't ask questions — pick a default and proceed. If you're truly done, just stop without making tool calls and the stagnation detector will release you.

## Debugging

```bash
# View hook log
keep-working log -n 100

# View all sessions
keep-working status

# Hook not firing? Check settings.json:
python3 -c "import json; print(json.load(open('$HOME/.claude/settings.json'))['hooks'])"

# Hook stopped working? May be the 48GB log bug:
ls -lh ~/.claude/hooks.log 2>/dev/null  # if GB-sized, delete it

# Start fresh:
keep-working clean
```

For detailed troubleshooting, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Caveats

1. **Process must be alive** — only works while Claude Code is running. Close the terminal and the timer is gone — there's no background daemon.
2. **Don't install as a plugin** — see issue #10412 above. Must be a raw skill in `~/.claude/skills/`.
3. **No questions in keep-working mode** — Claude is told not to ask. If it does, the hook overrides and forces continuation. To break out, say `stop keep working`.
4. **Token counts are approximate** — see Token counting section.
5. **Multiple concurrent sessions** — fully supported. Each session is isolated. Use `keep-working list` to see all.

## Credits

- Design inspiration from [`andylizf/nonstop`](https://github.com/andylizf/nonstop) (nudge_count + session-scoped pattern)
- Hook protocol from [Claude Code Hooks Guide](https://code.claude.com/docs/en/hooks-guide)

## File structure

```
cc-keep-working/
├── README.md                    # Entry point (bilingual links)
├── README_CN.md                 # Chinese docs
├── README_EN.md                 # This file
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE
├── docs/
│   ├── ARCHITECTURE.md          # Design decisions & trade-offs
│   └── TROUBLESHOOTING.md       # Debugging hook failures
└── keep-working/                # ← drop into ~/.claude/skills/
    ├── SKILL.md                 # Skill frontmatter + Claude instructions
    ├── install.sh / uninstall.sh
    ├── hooks/keep-working.py    # Stop + PreToolUse hook
    ├── bin/keep-working         # CLI helper (9 subcommands)
    ├── examples/                # demo.sh + benchmark.sh
    └── tests/                   # 61 unit tests
```

## License

MIT
