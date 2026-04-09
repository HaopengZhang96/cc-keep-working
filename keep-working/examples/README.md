# examples

Runnable scripts that exercise the keep-working hook without needing a
real Claude Code session. Both use a sandboxed `$HOME` — your real
`~/.claude/` is never touched.

## `demo.sh`

Full lifecycle demo:

1. Writes a `keep-working-pending.json` with an 8-second deadline.
2. Simulates the first tool call (PreToolUse → bind).
3. Fires Stop hook — shows the continuation message with exit 2.
4. Waits 9 seconds (past deadline).
5. Fires Stop hook again — shows exit 0 and cleared state.

Useful for "does it actually work on my machine?" confidence.

```bash
bash examples/demo.sh
```

## `benchmark.sh`

Micro-benchmark across realistic scenarios:

- `bind (no pending)` — startup overhead only
- `bind (claim pending)` — atomic claim path
- `stop (tiny transcript)` — baseline stop latency
- `stop (15MB cap-scan)` — first-scan on a big transcript
- `stop (delta, no growth)` — incremental path (the hot path)
- `200 sequential stops` — sustained throughput
- `20 concurrent binds` — contention / atomicity check

Expected per-op latencies are on the order of **10-30ms** depending on
what's in the transcript.

```bash
bash examples/benchmark.sh
```

Both scripts require `python3` on PATH. They clean up their sandboxes
on exit.
