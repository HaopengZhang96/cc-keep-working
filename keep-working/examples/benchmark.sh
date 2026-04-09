#!/usr/bin/env bash
# Micro-benchmark: measure keep-working hook latency.
# Runs against a sandboxed CLAUDE_HOME, no install required.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
HOOK="$SCRIPT_DIR/hooks/keep-working.py"

python3 - <<PY
import json, os, subprocess, tempfile, time, pathlib, shutil, hashlib

HOOK = "$HOOK"
SAND = pathlib.Path(tempfile.mkdtemp(prefix="kw-bench-"))
env = os.environ.copy()
env["HOME"] = str(SAND)
# Use a huge stagnation cap so benchmarks don't auto-release
env["KEEP_WORKING_STAGNATION_CAP"] = "100000"
env["KEEP_WORKING_NUDGE_CAP"] = "1000000"
(SAND / ".claude" / "keep-working").mkdir(parents=True)
PENDING = SAND / ".claude" / "keep-working-pending.json"

def run(sub, payload):
    return subprocess.run(["python3", HOOK, sub],
        input=json.dumps(payload), env=env, capture_output=True, text=True)

def write_pending(**kw):
    d = {"active": True, "deadline_epoch": time.time() + 3600,
         "max_turns": 1_000_000, "max_tokens": 100_000_000,
         "task": "bench", "created_at_epoch": time.time()}
    d.update(kw)
    PENDING.write_text(json.dumps(d))

def time_ms(fn, iters=20, warmup=3):
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    return (time.perf_counter() - t0) / iters * 1000

print("=" * 60)
print("  keep-working hook benchmark (local sandbox)")
print("=" * 60)

# 1. bind no-pending
t = time_ms(lambda: run("bind", {"session_id": "probe"}))
print(f"  bind (no pending)              {t:>6.1f} ms")

# 2. bind claim (write pending, bind it, clean)
def bind_cycle():
    write_pending()
    sid = f"b-{time.time_ns()}"
    run("bind", {"session_id": sid})
    for f in (SAND / ".claude" / "keep-working").glob("b-*.json"):
        f.unlink()
t = time_ms(bind_cycle)
print(f"  bind (claim pending)           {t:>6.1f} ms")

# Prepare an active session for stop benchmarks
write_pending()
run("bind", {"session_id": "bench-sid"})

# 3a. stop with tiny transcript
tiny = SAND / "tiny.jsonl"
tiny.write_text(json.dumps({"message": {"content": [{"type": "tool_use", "id": "t"}],
                                         "usage": {"input_tokens": 1000}}}) + "\n")
t = time_ms(lambda: run("stop", {"session_id": "bench-sid", "transcript_path": str(tiny)}))
print(f"  stop (tiny transcript)         {t:>6.1f} ms")

# 3b. stop with 15MB transcript, first-scan cap triggers
big = SAND / "big.jsonl"
junk = json.dumps({"message": {"content": [{"type": "text", "text": "x" * 800}]}})
with open(big, "w") as f:
    for _ in range(18_000):
        f.write(junk + "\n")
size_mb = big.stat().st_size / 1e6
# For first-scan timing we need to reset last_transcript_size each iteration
h = hashlib.sha1(b"bench-sid").hexdigest()[:16]
sf = SAND / ".claude" / "keep-working" / f"bench-sid_{h}.json"

def bench_first_scan():
    state = json.loads(sf.read_text())
    state["last_transcript_size"] = 0
    sf.write_text(json.dumps(state))
    run("stop", {"session_id": "bench-sid", "transcript_path": str(big)})
t = time_ms(bench_first_scan, iters=10)
print(f"  stop ({size_mb:.0f}MB cap-scan)         {t:>6.1f} ms")

# 3c. Incremental delta (only new content read)
t = time_ms(lambda: run("stop", {"session_id": "bench-sid", "transcript_path": str(big)}), iters=20)
print(f"  stop (delta, no growth)        {t:>6.1f} ms")

# 4. 200 sequential stops, appending a line each time
state = json.loads(sf.read_text())
state["nudge_count"] = 0
sf.write_text(json.dumps(state))
tstart = time.perf_counter()
for _ in range(200):
    with open(big, "a") as f:
        f.write(json.dumps({"message": {"content": [{"type": "tool_use", "id": "t"}],
                                         "usage": {"input_tokens": 500}}}) + "\n")
    run("stop", {"session_id": "bench-sid", "transcript_path": str(big)})
tdur = time.perf_counter() - tstart
print(f"  200 sequential stops (total)   {tdur*1000:>6.0f} ms  ({tdur*5:.1f} ms avg)")

# 5. 20 concurrent binds (race)
import threading
results = []
def one(i):
    write_pending(task=f"race-{i}")
    for f in (SAND / ".claude" / "keep-working").glob("race-*.json"):
        f.unlink()
    ts = []
    def bind_one(sid):
        run("bind", {"session_id": sid})
    for j in range(20):
        ts.append(threading.Thread(target=bind_one, args=(f"race-{i}-{j}",)))
    t0 = time.perf_counter()
    for t in ts: t.start()
    for t in ts: t.join()
    results.append(time.perf_counter() - t0)
for i in range(5):
    one(i)
print(f"  20 concurrent binds (median)   {sorted(results)[2]*1000:>6.0f} ms")

shutil.rmtree(SAND)
print()
print("  (Run from a fresh sandbox. Your real ~/.claude was not touched.)")
PY
