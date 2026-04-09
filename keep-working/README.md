# keep-working skill

This directory is the installable skill. For the full project documentation
(features, install instructions, CLI reference, caveats, credits), see the
[top-level README](../README.md).

## Quick install

```bash
bash install.sh
```

## Quick use

Say one of:

- `请持续工作 3 小时，重构 auth` (中文)
- `keep working for 3 hours on the refactor` (English)

To stop early:

- `停止持续工作` / `stop keep working`

## Files in this directory

| File | Purpose |
|---|---|
| `SKILL.md` | Skill frontmatter + instructions Claude reads when triggered |
| `hooks/keep-working.py` | PreToolUse (bind) + Stop hook script |
| `bin/keep-working` | CLI helper (9 subcommands: `status` [`--json`], `list`, `stop` [`--force`], `extend N` [`-s SID`], `config`, `doctor`, `clean`, `log` [`-n N`], `version`) |
| `install.sh` / `uninstall.sh` | Idempotent shell installers |
| `tests/test_hook.py` | 32 hook unittests (sandboxed) |
| `tests/test_cli.py` | 23 CLI unittests (sandboxed) |
| `examples/demo.sh` | Zero-setup end-to-end lifecycle demo in a temp `$HOME` |
| `README.md` | This file |

## CLI quick reference

```bash
keep-working doctor            # verify install is healthy
keep-working status            # show active sessions in detail
keep-working status --json     # machine-readable output for scripting
keep-working list              # terse one-liner per session
keep-working stop              # request active sessions to stop
keep-working extend 30         # add 30 minutes to all active sessions
keep-working extend -15 -s abc # shorten specific session by 15 min
keep-working config            # show env var configuration
keep-working log -n 100        # tail hook log
keep-working clean             # wipe all state files (emergency reset)
```

## Run tests

```bash
python3 tests/test_hook.py   # 32 hook tests
python3 tests/test_cli.py    # 23 CLI tests
# or run both via unittest discovery
python3 -m unittest discover tests
```

All tests use a sandboxed `$HOME`, so they don't touch your real `~/.claude/`.

## Reporting issues

See the top-level README for the project repository URL.
