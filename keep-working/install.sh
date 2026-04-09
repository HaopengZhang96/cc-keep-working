#!/usr/bin/env bash
# Idempotent installer for the keep-working skill.
#
# What it does:
#   1. Copy this skill to ~/.claude/skills/keep-working/
#   2. Copy the hook script to ~/.claude/hooks/keep-working.py
#   3. Merge PreToolUse + Stop hooks into ~/.claude/settings.json,
#      preserving any existing hooks (uses python json, not jq).
#
# Re-running is safe: existing files are overwritten with the latest
# version, and the settings.json merge is idempotent (won't duplicate
# entries).
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CLAUDE_DIR="${CLAUDE_HOME:-$HOME/.claude}"
SKILLS_DIR="$CLAUDE_DIR/skills/keep-working"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SETTINGS="$CLAUDE_DIR/settings.json"

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    if command -v python >/dev/null 2>&1; then
        PYTHON_BIN=python
    else
        echo "ERROR: python3 (or python) is required." >&2
        exit 1
    fi
fi

echo "==> Installing keep-working skill"
echo "    skill source : $SCRIPT_DIR"
echo "    target dir   : $CLAUDE_DIR"
echo "    python       : $PYTHON_BIN"

mkdir -p "$SKILLS_DIR" "$HOOKS_DIR" "$SKILLS_DIR/hooks" "$CLAUDE_DIR/keep-working"

# 1. Copy skill files
cp "$SCRIPT_DIR/SKILL.md" "$SKILLS_DIR/SKILL.md"
cp "$SCRIPT_DIR/hooks/keep-working.py" "$SKILLS_DIR/hooks/keep-working.py"
[ -f "$SCRIPT_DIR/README.md" ] && cp "$SCRIPT_DIR/README.md" "$SKILLS_DIR/README.md"

# 2. Copy hook script to active hooks dir
cp "$SCRIPT_DIR/hooks/keep-working.py" "$HOOKS_DIR/keep-working.py"
chmod +x "$HOOKS_DIR/keep-working.py" || true

# 3. Copy CLI helper if present
if [ -f "$SCRIPT_DIR/bin/keep-working" ]; then
    mkdir -p "$SKILLS_DIR/bin"
    cp "$SCRIPT_DIR/bin/keep-working" "$SKILLS_DIR/bin/keep-working"
    chmod +x "$SKILLS_DIR/bin/keep-working" || true
fi

# 3b. Copy example scripts (demo.sh, benchmark.sh) for user reference.
if [ -d "$SCRIPT_DIR/examples" ]; then
    mkdir -p "$SKILLS_DIR/examples"
    cp -f "$SCRIPT_DIR/examples/"*.sh   "$SKILLS_DIR/examples/" 2>/dev/null || true
    cp -f "$SCRIPT_DIR/examples/"*.md   "$SKILLS_DIR/examples/" 2>/dev/null || true
    chmod +x "$SKILLS_DIR/examples/"*.sh 2>/dev/null || true
fi

# 4. Merge settings.json. We use the same PYTHON_BIN we resolved above
# so the settings.json doesn't hardcode a python3 that may not exist on
# the user's PATH.
"$PYTHON_BIN" - "$SETTINGS" "$PYTHON_BIN" <<'PY'
import json, os, sys
path = sys.argv[1]
py = sys.argv[2]
hook_cmd_pre  = f"{py} ~/.claude/hooks/keep-working.py bind"
hook_cmd_stop = f"{py} ~/.claude/hooks/keep-working.py stop"

if os.path.exists(path):
    try:
        with open(path) as f:
            settings = json.load(f)
    except Exception:
        # Back up unreadable settings before overwriting.
        bak = path + ".bak"
        os.rename(path, bak)
        print(f"    WARN: existing settings.json was unreadable; backed up to {bak}")
        settings = {}
else:
    settings = {}

settings.setdefault("hooks", {})

def ensure(event, arg_cmd):
    arr = settings["hooks"].setdefault(event, [])
    # Look for an existing matcher:"" group; create if missing.
    group = None
    for g in arr:
        if g.get("matcher", "") == "":
            group = g
            break
    if group is None:
        group = {"matcher": "", "hooks": []}
        arr.append(group)
    group.setdefault("hooks", [])
    # Idempotency: don't add a duplicate of our hook.
    for h in group["hooks"]:
        if h.get("command", "").endswith("/keep-working.py " + arg_cmd.split()[-1]):
            return False
        if h.get("command") == arg_cmd:
            return False
    group["hooks"].append({"type": "command", "command": arg_cmd})
    return True

added_pre  = ensure("PreToolUse", hook_cmd_pre)
added_stop = ensure("Stop",       hook_cmd_stop)

with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"    settings.json: PreToolUse {'added' if added_pre else 'already present'}, "
      f"Stop {'added' if added_stop else 'already present'}")
PY

echo
echo "✓ Installed."
echo

# 5. Run doctor sanity check if CLI is present.
if [ -x "$SKILLS_DIR/bin/keep-working" ]; then
    echo "==> Running doctor sanity check"
    "$SKILLS_DIR/bin/keep-working" doctor || {
        echo
        echo "⚠  Doctor reported issues above. Install files were copied but"
        echo "   the environment may need fixing. Re-run 'keep-working doctor'"
        echo "   after addressing them."
    }
    echo
fi

echo "Restart Claude Code, then try:"
echo "    \"请持续工作 0.1 小时，每 30 秒往 /tmp/heartbeat.log 追加一行时间戳\""
echo
echo "To uninstall: $SCRIPT_DIR/uninstall.sh"
