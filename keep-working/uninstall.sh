#!/usr/bin/env bash
# Uninstaller for keep-working skill.
#
# Removes:
#   - ~/.claude/skills/keep-working/
#   - ~/.claude/hooks/keep-working.py
#   - keep-working hook entries from ~/.claude/settings.json (preserves others)
#   - any leftover state files in ~/.claude/keep-working/
#   - ~/.claude/keep-working-pending.json
#   - ~/.claude/keep-working-stop-request
set -euo pipefail

CLAUDE_DIR="${CLAUDE_HOME:-$HOME/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    if command -v python >/dev/null 2>&1; then PYTHON_BIN=python
    else echo "ERROR: python required" >&2; exit 1; fi
fi

echo "==> Uninstalling keep-working"

rm -rf "$CLAUDE_DIR/skills/keep-working"
rm -f  "$CLAUDE_DIR/hooks/keep-working.py"
rm -rf "$CLAUDE_DIR/keep-working"
rm -f  "$CLAUDE_DIR/keep-working-pending.json"
rm -f  "$CLAUDE_DIR/keep-working-stop-request"

if [ -f "$SETTINGS" ]; then
    "$PYTHON_BIN" - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        settings = json.load(f)
except Exception:
    sys.exit(0)

hooks = settings.get("hooks", {})
changed = False
for event in list(hooks.keys()):
    new_groups = []
    for group in hooks[event]:
        new_h = []
        for h in group.get("hooks", []):
            cmd = h.get("command", "")
            if "keep-working.py" in cmd:
                changed = True
                continue
            new_h.append(h)
        if new_h:
            group["hooks"] = new_h
            new_groups.append(group)
    if new_groups:
        hooks[event] = new_groups
    else:
        hooks.pop(event)
        changed = True
if not hooks:
    settings.pop("hooks", None)

if changed:
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("    settings.json: hook entries removed")
else:
    print("    settings.json: no keep-working hooks to remove")
PY
fi

echo "✓ Uninstalled."
