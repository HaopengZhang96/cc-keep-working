# Contributing to keep-working

Thanks for wanting to improve keep-working. Quick notes to keep PRs smooth.

## Repo layout

```
.
├── README.md               # project docs (bilingual)
├── CHANGELOG.md
├── LICENSE                 # MIT
├── CONTRIBUTING.md         # this file
├── .github/workflows/      # CI
└── keep-working/           # ← the skill itself; copied to ~/.claude/skills/
    ├── SKILL.md            # what Claude reads on invocation
    ├── README.md           # per-skill quick reference
    ├── install.sh          # idempotent installer
    ├── uninstall.sh        # idempotent uninstaller
    ├── hooks/keep-working.py   # Stop + PreToolUse hook
    ├── bin/keep-working        # CLI helper
    └── tests/test_hook.py      # unittests (sandboxed, safe to run anywhere)
```

## Running tests

```bash
python3 keep-working/tests/test_hook.py
```

Tests override `$HOME` to a temp dir, so they never touch your real
`~/.claude/`. Safe to run on any machine. Takes ~1 second.

## Manual smoke test

```bash
# Install into a throwaway CLAUDE_HOME
SANDBOX=$(mktemp -d)
echo '{}' > $SANDBOX/settings.json
CLAUDE_HOME=$SANDBOX bash keep-working/install.sh
cat $SANDBOX/settings.json
# ... exercise hooks manually ...
CLAUDE_HOME=$SANDBOX bash keep-working/uninstall.sh
rm -rf $SANDBOX
```

## Guidelines

- **Tests for all behavior changes.** If you add a feature or fix a bug in
  `hooks/keep-working.py`, add a test case to `tests/test_hook.py`. CI runs
  on push and PR.
- **Hook must never crash Claude Code.** The `bind` subcommand should
  always exit 0 — any internal error is caught and logged. The `stop`
  subcommand should fall back to exit 0 (allow stop) on unexpected errors
  rather than blocking indefinitely.
- **No new Python deps.** Stdlib only. The hook runs on every PreToolUse
  and every Stop — adding `import requests` or similar would bloat startup
  time and create install headaches.
- **Small, composable diffs.** Bug fix → separate PR from feature.
- **Update CHANGELOG.md** in the same PR as your code change. Follow the
  existing format (Fixed / Added / Changed sections, latest version on
  top).
- **Preserve backwards compatibility** of state file format within a major
  version if practical. v0 → v0.1 → v0.2 have each broken the format — if
  you do this again, bump to v0.3 and mention it in CHANGELOG with
  migration notes.

## Design principles

1. **Fail open, not closed.** When in doubt, allow the stop and clear
   state. A wrongly-released session wastes a minute; a wrongly-blocked
   session with a bug can wedge Claude for hours.
2. **Bounded resources.** Log file size-capped, transcript scan byte-
   capped, nudge count capped, hours capped. Every cap is a defense
   against a future bug turning into a quota or disk disaster.
3. **Multi-session first.** Never assume there's only one keep-working
   session running. State files are keyed by session_id hash.
4. **Local only.** No network calls, no telemetry, no external deps.
5. **Explicit > implicit.** Trigger phrases must include a duration.
   Caps have hard ceilings even if the user asks for more.

## Issue templates

See `.github/ISSUE_TEMPLATE/`.

## License

MIT (see LICENSE).
