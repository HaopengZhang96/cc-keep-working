# Troubleshooting keep-working

## First: run `keep-working doctor`

```bash
~/.claude/skills/keep-working/bin/keep-working doctor
```

This checks: hook script exists + is executable, skill dir present,
settings.json has both hooks registered and parseable, python3 on PATH,
sessions dir writable, hook runs without crashing, log file size sane.
Most issues are one of these.

## Claude Code just stops after a few minutes anyway

**Check**: `keep-working log -n 50` to see if the Stop hook is firing at
all.

Common causes:
1. **Hook not registered** — `settings.json` doesn't have `hooks.Stop`
   pointing at the script. Fix: re-run `install.sh`.
2. **Installed as a plugin** — exit-code-2 hooks don't work from the
   Claude Code plugin system (see
   [claude-code#10412](https://github.com/anthropics/claude-code/issues/10412)).
   Fix: install the raw skill to `~/.claude/skills/` and the hook to
   `~/.claude/hooks/`.
3. **48GB `~/.claude/hooks.log`** — this breaks ALL hooks silently. See
   [claude-code#16047](https://github.com/anthropics/claude-code/issues/16047).
   Fix: `rm ~/.claude/hooks.log`.
4. **Wrong session bound** — `keep-working status` shows no session or
   a different one than you expect. The pending file may have been
   claimed by an earlier tool call. Fix: start a new keep-working
   request and make sure the very next tool call is the one you want.
5. **Stagnation auto-release** — Claude made no tool calls in the last
   3 turns, so the hook released the session. This is intentional.
   Check `empty_stops` in the session file; if it's 3, stagnation
   released.

## Hook is blocking but the message looks wrong

Read the log:
```bash
keep-working log -n 100 | grep -v "^$"
```

Each stop writes a line like:
```
2026-04-09 11:00:00 stop: blocked sid=9634dbbc nudge=5 empty=0 ctx=12345
```

If you see `nudge_count` climbing but no sub-task progress, Claude is
looping without doing real work. Consider shortening the deadline or
stopping manually.

## Multiple sessions are interfering

Run `keep-working list` to see all active sessions. Each should have a
distinct `session_id`. If you see duplicates or cross-contamination:

```bash
keep-working clean   # nuclear option: wipe all state
```

Then restart Claude Code fresh.

## Session started but CLI shows "no sessions"

This means the pending file was never bound. Check:

```bash
ls -la ~/.claude/keep-working-pending.json
```

If it exists: Claude wrote the pending file but never made a tool call
that fired the bind hook. Most likely Claude is waiting on you. Tell
Claude to "continue" or make any request that triggers a tool call.

If it doesn't exist but CLI shows nothing: the state file may have been
deleted (deadline hit, manual stop, or orphan sweep). Start a new
session.

## "`python3`: command not found" from settings.json

Edit `~/.claude/settings.json` and change the hook commands from
`python3 ~/.claude/hooks/keep-working.py ...` to whatever Python
interpreter you have (`python` on some systems, `py -3` on Windows,
`/usr/bin/env python3` as a more portable fallback).

## Stop hook fires but Claude stops anyway

The hook might be exiting 2 but Claude Code's continuation path might
not be triggering. Rare but possible:

```bash
# Manually test the hook
echo '{"session_id":"test","transcript_path":"/tmp/nothing"}' | \
    python3 ~/.claude/hooks/keep-working.py stop
echo "Exit: $?"
```

Expected: exit 0 (no state file for "test" → allow stop).

```bash
# Now with a real state file
keep-working status
# Pick a session_id from the output, substitute below:
echo '{"session_id":"YOUR_SID","transcript_path":"/tmp/nothing"}' | \
    python3 ~/.claude/hooks/keep-working.py stop
echo "Exit: $?"
```

Expected: exit 2 + the continuation message on stderr.

If this works manually but not from Claude Code: file a bug.

## "settings.json has hooks but they're not running"

Check settings.json JSON validity:

```bash
python3 -c "import json; json.load(open('$HOME/.claude/settings.json'))"
```

If that errors, fix the JSON.

## Resetting everything

```bash
~/.claude/skills/keep-working/bin/keep-working clean
rm -rf ~/.claude/keep-working/  # wipe session files and log
~/.claude/skills/keep-working/uninstall.sh
bash <path-to-repo>/keep-working/install.sh
```

## Still stuck?

Open an issue with the output of:
- `keep-working version`
- `keep-working doctor`
- `keep-working log -n 50`
- `cat ~/.claude/settings.json` (redact secrets)
- OS and Claude Code version

Templates are in `.github/ISSUE_TEMPLATE/`.
