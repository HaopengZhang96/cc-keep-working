#!/usr/bin/env bash
# Demo: exercise the full keep-working lifecycle in a sandboxed $HOME, no
# Claude Code required. Useful for "does the hook actually work on my
# machine?" confidence-building.
#
# Usage: bash examples/demo.sh
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
HOOK="$SCRIPT_DIR/hooks/keep-working.py"

SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

export HOME="$SANDBOX"
mkdir -p "$SANDBOX/.claude/keep-working"
cp "$HOOK" "$SANDBOX/.claude/hooks-keep-working.py"
HOOK_COPY="$SANDBOX/.claude/hooks-keep-working.py"

SESSION_ID="demo-$(date +%s)"
NOW=$(python3 -c 'import time; print(int(time.time()))')
DEADLINE=$((NOW + 8))

echo "1. Write pending state (deadline in 8 seconds)"
cat > "$SANDBOX/.claude/keep-working-pending.json" <<JSON
{
  "active": true,
  "deadline_epoch": $DEADLINE,
  "max_turns": 200,
  "max_tokens": 5000000,
  "task": "demo task",
  "created_at_epoch": $NOW
}
JSON

echo "2. Simulate first tool call: PreToolUse bind"
echo "{\"session_id\":\"$SESSION_ID\"}" | python3 "$HOOK_COPY" bind
ls "$SANDBOX/.claude/keep-working/"

# Build a tiny transcript JSONL
TR="$SANDBOX/transcript.jsonl"
printf '{"message":{"content":[{"type":"tool_use","id":"t"}],"usage":{"input_tokens":1000}}}\n' > "$TR"

echo "3. First Stop attempt (deadline not hit) — expect exit 2 + 'DO NOT STOP' on stderr"
set +e
echo "{\"session_id\":\"$SESSION_ID\",\"transcript_path\":\"$TR\"}" | python3 "$HOOK_COPY" stop
echo "   (hook exit: $?)"
set -e

echo "4. Waiting 9 seconds for deadline to pass..."
sleep 9

echo "5. Second Stop attempt (deadline passed) — expect exit 0, state cleared"
set +e
echo "{\"session_id\":\"$SESSION_ID\",\"transcript_path\":\"$TR\"}" | python3 "$HOOK_COPY" stop
echo "   (hook exit: $?)"
set -e
ls "$SANDBOX/.claude/keep-working/" 2>&1 || true

echo
echo "✓ demo complete. Temp files cleaned up."
