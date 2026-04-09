# Changelog

## 0.2.0 — 2026-04-09

Substantial rewrite. Thanks to self-audit + research on
[`andylizf/nonstop`](https://github.com/andylizf/nonstop) and related
Claude Code hook issues.

### Fixed

- **Token counting was double-counting by an order of magnitude.** v0.1
  summed `usage.input_tokens` across all JSONL entries, but each API
  call's `input_tokens` already includes the full prior context.
  v0.2 takes the MAX of `input + output + cache_creation + cache_read`
  over all messages, which actually approximates "current context used".
- **Early stop was broken.** v0.1 asked Claude to find its own session
  file by fuzzy match — Claude doesn't know its own `session_id`. v0.2
  adds a `~/.claude/keep-working-stop-request` flag processed on the
  next PreToolUse bind.
- **Silent hook failures** — v0.1 exited 0 on every internal error with
  no trace. v0.2 logs to `~/.claude/keep-working/log.txt` (size-capped
  at 1MB to avoid the
  [48GB log bug](https://github.com/anthropics/claude-code/issues/16047)).
- **README install paths were inconsistent** (`keep-working/keep-working`
  nesting confusion). Fixed + added `install.sh`.
- **session_id sanitization could collide** (two distinct ids → same
  filename). Now hashed via SHA1.

### Added

- **Stagnation detection**: if 3 consecutive Stop events happen with no
  new tool calls in between, release the session. Prevents the
  "already done but hook keeps stuffing 'keep going'" failure mode.
  Configurable via `KEEP_WORKING_STAGNATION_CAP`.
- **Orphan sweep**: session files older than 24h are removed on each
  bind. Configurable via `KEEP_WORKING_ORPHAN_TTL_SEC`.
- **Plain-text stderr output** (not JSON): empirically Claude Code's
  current Stop-hook path shows the full stderr to the user, so a
  JSON-wrapped `{"decision":"block","reason":"..."}` would produce ugly
  double-display. Keeping plain text until the future parser path in
  [claude-code#10412](https://github.com/anthropics/claude-code/issues/10412)
  stabilizes.
- **Minute support** in trigger phrases (`30 分钟`, `90 min`, `1.5h`).
- **Autonomous mode**: SKILL.md now tells Claude not to ask questions
  during keep-working mode — pick a default and proceed.
- **`install.sh` / `uninstall.sh`**: idempotent shell installers that
  merge settings.json via Python (no jq needed), preserving existing
  hooks.
- **CLI helper `bin/keep-working`** with 9 subcommands:
  `status`, `list`, `stop`, `extend` (±minutes), `config`, `doctor`,
  `clean`, `log`, `version`.
- **Bilingual continuation messages**: if the task description contains
  CJK characters, the hook's injected "keep going" reason is in Chinese;
  otherwise English. Detected per-invocation.
- **Incremental (delta) transcript scan**: keep-working records the byte
  offset of each Stop's scan and only reads new content on the next one.
  First-scan on a 14MB transcript takes ~33ms (capped at last 5MB);
  subsequent stops read only the delta (typically 200B-2KB) in ~19ms.
  This replaces the earlier "scan last 5MB every time" approach, which
  had a subtle bug where the sliding scan window could drop old tool_use
  blocks and cause false stagnation releases. Configurable via
  `KEEP_WORKING_SCAN_MAX_BYTES`.
- **File-size-based progress tracking, then reverted**: investigated
  using transcript file-size growth as the stagnation signal (simpler
  than tool_use counting) but discovered that Claude Code writes an
  assistant turn on every stop, making file size grow unconditionally
  and breaking stagnation. Reverted to tool_use counting in the delta
  window (which works correctly because the delta captures only the
  most recent turn).
- **`install.sh` runs doctor** at the end to surface any environment
  issues immediately.
- **FAQ + worked example** in SKILL.md so Claude handles edge cases
  correctly (no task given, >24h request, multi-session, extend, etc.).
- **GitHub Actions CI** runs tests on macOS + Ubuntu × Python 3.9-3.12,
  plus end-to-end install.sh / uninstall.sh sandbox verification.
- **`CONTRIBUTING.md`**, `LICENSE` (MIT), issue templates, `.gitignore`.
- **`examples/demo.sh`**: zero-setup end-to-end lifecycle demo in a
  temp `$HOME` — useful for "does it work on my machine?" confidence.
- **Test suite**: 54 unittests total, all with sandboxed `$HOME` / `CLAUDE_HOME`:
  - `tests/test_hook.py` — 31 tests: bind / stop / env vars /
    stagnation (text-only turns + large-transcript regression) /
    token cap MAX-not-SUM / i18n (CN/EN) / 10MB streaming scan /
    orphan sweep / path traversal / corrupt files / concurrent-bind
    atomicity (20 parallel).
  - `tests/test_cli.py` — 23 tests: version / status (+ `--json`) /
    list / stop (+ `--force` guard) / clean / extend (+ specific-session
    + negative) / config / log / doctor.
- **state files `chmod 600`** — task descriptions may contain sensitive
  info.
- **`nudge_count` defense in depth** — even if user sets max_turns too
  high, a hard nudge cap (default 500, `KEEP_WORKING_NUDGE_CAP` env)
  prevents runaway.
- **Deadline horizon clamping** — SKILL.md says "24h max hours" but is
  trust-based. The hook now independently clamps any deadline that's
  more than 25 hours in the future (1h buffer). Env:
  `KEEP_WORKING_MAX_HORIZON_SEC`.
- **Hook version flag** — `python3 keep-working.py --version` (or `-V` /
  `version`) prints the hook version. Doctor cross-checks CLI vs hook
  versions and flags a mismatch if you forget to rerun install.sh
  after pulling a new version.
- **`keep-working doctor --quiet`** / `-q` — suppress output, exit
  code only. Useful for CI health checks.
- **`install.sh` also copies `examples/`** (`demo.sh`, `benchmark.sh`,
  `README.md`) so users get runnable examples without needing the repo.

### Changed

- State file path scheme: was `~/.claude/keep-working-state.json`
  (single global file, v0), then `~/.claude/keep-working/<sid>.json`
  (v0.1, with sanitized sid), now
  `~/.claude/keep-working/<safe_prefix>_<hash>.json` (v0.2).
- Stop-hook continuation message is more direct: emphasizes "DO NOT
  STOP", tells Claude to pick a default instead of asking, and explains
  the stagnation-detector escape hatch.

### Migration notes (v0.1 → v0.2)

- **Rerun `install.sh`**. It is idempotent and will overwrite the old
  hook script and add the new PreToolUse `bind` hook.
- **The state file location changed**. v0.1 used a single
  `~/.claude/keep-working-state.json`. v0.2 uses per-session files in
  `~/.claude/keep-working/`. Any leftover v0.1 state file is harmless
  but no longer read; `keep-working clean` will remove it.
- **Trigger phrases and hard caps are the same**. No changes needed to
  how you invoke the skill.
- **If you had other `Stop` hooks in settings.json**, the new
  `install.sh` preserves them. v0.1's single-hook merge was fragile;
  v0.2 uses a Python-based idempotent merge that won't clobber.
- **CLI is new in v0.2**. Run `~/.claude/skills/keep-working/bin/keep-working doctor`
  after upgrade to verify everything is wired.

## 0.1.0

Initial release. Single Stop hook + global state file.
