#!/usr/bin/env python
"""asyncio + httpx load harness for the Clinical Co-Pilot sidecar POST /chat.

Primary tool for this repo is the k6 script (chat_load.js, run via Docker). This
Python harness is the documented fallback (works if the k6 container cannot reach
the host) AND the tool used for the two low-count / paced measurements where a
precise request budget matters:

  - the clean app+DB latency baseline (paced under the app's 30 req/60s per-IP
    rate limit, so percentiles are not polluted by 429 rejections), and
  - the short real-LLM sample (bounded request count to cap token spend).

Examples (run with the repo venv python):
  # clean mock baseline: 25 reqs, concurrency 2, paced to stay under the limiter
  python load.py --url http://127.0.0.1:8809 --n 25 --concurrency 2 --min-interval 2.0

  # short real-LLM sample: 10 reqs, concurrency 2 (10 < 30 so no 429)
  python load.py --url http://127.0.0.1:8808 --n 10 --concurrency 2

  # raw throughput / stress (will hit the 30/min per-IP 429 wall by design)
  python load.py --url http://127.0.0.1:8809 --duration 30 --concurrency 50
"""
import argparse
import asyncio
import os
import random
import sys
import time

import httpx

# Make the sidecar's `app` package importable so we can mint real session tokens
# from the same signing key the running sidecar verifies against (AUTH_TOKEN_SPEC).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.config import get_settings  # noqa: E402
from app.session_token import get_signing_key, mint  # noqa: E402

PIDS = [1, 101, 102]
MESSAGES = ["brief me", "what changed", "summarize recent labs"]

# Mint one patient-scoped token PER pid up front: the sidecar binds each token to
# a single patient (a token for patient A can never act on B), so per-pid tokens
# are required. Reads the DB signing key (or the dev fallback under
# COPILOT_AUTH_DEV_MODE) — the same key the sidecar uses.
_S = get_settings()
_KEY = get_signing_key()
_NOW = int(time.time())
TOKENS = {
    pid: mint(
        sub=_S.demo_user_id, usr="load", pid=pid, scope="patient",
        acl="patients:demo", key=_KEY, ttl=_S.token_ttl_s, now=_NOW,
    )
    for pid in PIDS
}


def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


async def worker(client, url, stop_at, remaining, min_interval, results, lock):
    while True:
        async with lock:
            if remaining["n"] <= 0:
                return
            remaining["n"] -= 1
        if stop_at is not None and time.time() >= stop_at:
            return
        pid = random.choice(PIDS)
        body = {"pid": pid, "message": random.choice(MESSAGES)}
        t0 = time.perf_counter()
        status = None
        try:
            r = await client.post(
                f"{url}/chat",
                json=body,
                headers={"Authorization": f"Bearer {TOKENS[pid]}"},
                timeout=60.0,
            )
            status = r.status_code
            ok = status == 200 and "correlation_id" in r.text
        except Exception:
            ok = False
        dt = (time.perf_counter() - t0) * 1000.0
        results.append((dt, ok, status))
        if min_interval > 0:
            await asyncio.sleep(min_interval)


async def run(args):
    remaining = {"n": args.n if args.duration is None else 10**12}
    stop_at = time.time() + args.duration if args.duration is not None else None
    results = []
    lock = asyncio.Lock()
    start = time.perf_counter()
    async with httpx.AsyncClient(http2=False) as client:
        tasks = [
            asyncio.create_task(
                worker(client, args.url, stop_at, remaining, args.min_interval, results, lock)
            )
            for _ in range(args.concurrency)
        ]
        await asyncio.gather(*tasks)
    wall = time.perf_counter() - start

    lat_all = sorted(d for d, _, _ in results)
    lat_ok = sorted(d for d, ok, _ in results if ok)
    total = len(results)
    ok_n = sum(1 for _, ok, _ in results if ok)
    err_n = total - ok_n
    codes = {}
    for _, _, s in results:
        codes[s] = codes.get(s, 0) + 1

    print(f"\n=== {args.url}  n={total}  concurrency={args.concurrency}  wall={wall:.1f}s ===")
    print(f"throughput: {total / wall:.1f} req/s   (ok-only: {ok_n / wall:.1f} req/s)")
    print(f"error rate: {err_n / total * 100:.2f}%   ({err_n}/{total})   status codes: {codes}")
    print("latency ms  |  ALL requests           |  200-only (app+DB path)")
    for label, p in [("p50", 50), ("p95", 95), ("p99", 99)]:
        print(f"  {label:<4}      |  {pct(lat_all, p):>10.1f}            |  {pct(lat_ok, p):>10.1f}")
    if lat_all:
        print(f"  min/max   |  {lat_all[0]:.1f} / {lat_all[-1]:.1f} ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8809")
    ap.add_argument("--n", type=int, default=25, help="total requests (ignored if --duration set)")
    ap.add_argument("--duration", type=float, default=None, help="run for N seconds instead of fixed count")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--min-interval", type=float, default=0.0, help="per-worker sleep between reqs (pacing)")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
