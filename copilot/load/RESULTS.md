# Load / stress baselines — Clinical Co-Pilot sidecar `POST /chat`

Local baselines captured 2026-07-09. Tool used for the throughput runs: **k6**
(via Docker, container reached the host fine — the Python fallback was not
needed for these). The two low-count / paced measurements (clean latency
baseline, real-LLM sample) use the **`load.py`** asyncio+httpx harness because
they need a precise request budget.

Scripts in this folder:
- `chat_load.js` — k6 script (primary). Emits p50/p95/p99 via `summaryTrendStats`.
- `load.py` — asyncio+httpx harness (documented k6 fallback; also used for the
  paced baseline and the token-bounded real-LLM sample).

---

## Headline finding — single-worker saturation at ~4 req/s

With the per-IP rate limit disabled (`COPILOT_RATE_LIMIT_PER_MIN=1000000`), all
requests now reach the app+DB path — **error rate ~0%**. The binding ceiling is
then the sidecar's own throughput: it plateaus at **~4 req/s**, essentially flat
from 10 → 50 VUs, while latency grows with the queue depth. That is the
signature of a **single-worker** server: adding concurrency does not raise
throughput, it just deepens the queue until requests time out.

> Earlier runs (rate limiter at its default 30 req/60 s per IP) showed the app
> shedding single-IP bursts as fast **~3 ms HTTP 429 `rate_limited`** responses
> — load-shedding working as designed, ~99% "error" rate by rejection, server
> unaffected. Those throttled numbers are superseded by the clean runs below.

---

## Mock server (port 8809) — infra throughput baseline (no token cost, no throttle)

Mock mode stubs the LLM, so these isolate the FastAPI app + Authorization
Gateway + read-only MariaDB path from LLM latency/cost. Rate limit disabled.

### mock @ 10 VUs, 30 s (k6, `constant-vus`)

| metric | value |
|---|---|
| throughput | 139 reqs, **4.40 req/s** |
| error rate | **0.00%** (0 / 139) |
| latency p50 / p95 / p99 | **2.13 s / 3.20 s / 3.38 s** (min 1.58 / max 3.86 s) |

### mock @ 50 VUs, 30 s (k6, `constant-vus`)

| metric | value |
|---|---|
| throughput | 161 reqs, **3.63 req/s** |
| error rate | **2.48%** (4 / 161) |
| latency p50 / p95 / p99 | **9.95 s / 25.88 s / 29.99 s** (min 3.75 / max 29.99 s) |

Throughput is flat (4.40 → 3.63 req/s) from 10 → 50 VUs while p95 climbs from
3.2 s to 25.9 s: the server saturates at ~4 req/s and the extra 40 VUs only
queue. The 4 errors at 50 VUs are **client-side 30 s timeouts** (max = 29.99 s =
the k6 per-request `timeout: '30s'`) on requests stuck at the tail of the queue —
i.e. saturation timeouts, **not** 429s. At 10 VUs the queue stays shallow enough
that error rate is a clean **0%**. Provisioning note: raise worker/process count
(e.g. uvicorn `--workers`, or horizontal replicas) to lift the ~4 req/s ceiling.

### Clean mock baseline — uncontended app+DB per-request cost

`load.py`, loopback 8809, concurrency 1, paced under the limiter (`--min-interval 2.5`), n=20:

| metric | value |
|---|---|
| requests / errors | 20 / **0.00%** (all 200) |
| latency p50 / p95 / p99 | **460 ms / 579 ms / 629 ms** (min 423 / max 641 ms) |

This is the true single-request app+DB compute cost with no LLM and no queueing.

---

## Real-LLM sample (port 8808) — realistic end-to-end

`load.py`, loopback 8808, concurrency 2, n=10 (kept small to bound token spend):

| metric | value |
|---|---|
| requests / errors | 10 / **0.00%** (all 200) |
| throughput | ~0.2 req/s |
| latency p50 / p95 / p99 | **10.35 s / 12.71 s / 13.61 s** (min 5.88 / max 13.83 s) |

Latency is dominated by the live LLM call; app+DB (~0.5 s from the mock baseline)
is a small fraction of end-to-end time.

---

## Local process CPU / RSS sample (mock sidecar, during a clean 50-VU run)

Sidecar processes: **pid 16932 = mock (8809)**, **pid 14824 = real (8808)**.
The two ~4.7 MB `python.exe` (pids 10076, 16552) are the uvicorn `--reload`
parent watchers.

Sampled during a clean (non-throttled) mock **50-VU** run — the app+DB path is
actually driven here (mock sidecar = pid 18564 after the coordinator's restart;
`cpu_s` is cumulative, `d` = delta between 3 s samples):

```
16:07:03 | pid18564 (mock): cpu_s=1054.7 (d=-)   rss=408.2MB
16:07:06 | pid18564 (mock): cpu_s=1054.8 (d=0.1) rss=408.2MB
16:07:09 | pid18564 (mock): cpu_s=1054.8 (d=0.0) rss=408.2MB
16:07:12 | pid18564 (mock): cpu_s=1054.8 (d=0.0) rss=408.2MB
16:07:15 | pid18564 (mock): cpu_s=1055.0 (d=0.2) rss=408.2MB
16:07:18 | pid18564 (mock): cpu_s=1055.1 (d=0.1) rss=408.2MB
16:07:21 | pid18564 (mock): cpu_s=1055.2 (d=0.1) rss=408.2MB
```

- **Mock sidecar RSS:** steady ~408 MB under sustained 50-VU load (no growth
  across the run — no obvious leak over 30 s).
- **CPU under clean 50-VU saturation:** only ~0.1–0.2 cpu-seconds per 3 s
  ≈ **~5% of one core**. The server is saturated at ~4 req/s yet barely uses
  CPU, so the throughput ceiling is **not CPU-bound** — it is a single-worker
  I/O / serialization bottleneck (the request spends its time awaiting the DB
  round-trip, not computing). Implication: the fix is more workers/replicas, and
  there is ample CPU headroom on this host.

---

## Reproduce

From `copilot/load/` (mock server on 8809, real on 8808; both need
`Authorization: Bearer demo-local-secret-change-me`).

```bash
# k6 mock @ 10 VUs, 30 s  (Docker; container -> host.docker.internal:8809)
docker run --rm -i --add-host=host.docker.internal:host-gateway \
  -e SCENARIO=constant -e VUS=10 -e DURATION=30s grafana/k6 run - <chat_load.js

# k6 mock @ 50 VUs, 30 s
docker run --rm -i --add-host=host.docker.internal:host-gateway \
  -e SCENARIO=constant -e VUS=50 -e DURATION=30s grafana/k6 run - <chat_load.js

# (single k6 invocation that ramps 10 -> 50: omit SCENARIO, default 'stages')
docker run --rm -i --add-host=host.docker.internal:host-gateway grafana/k6 run - <chat_load.js

# Clean uncontended app+DB baseline (concurrency 1; --min-interval paces under
# the limiter when it is enabled — harmless when it is disabled)
../.venv/Scripts/python.exe load.py --url http://127.0.0.1:8809 --n 20 --concurrency 1 --min-interval 2.5

# Short real-LLM sample (bounded request count)
../.venv/Scripts/python.exe load.py --url http://127.0.0.1:8808 --n 10 --concurrency 2 --min-interval 0.5

# CPU / RSS sampling during a run (PowerShell)
for ($i=0;$i -lt 6;$i++){ Get-Process python | ForEach-Object { "$($_.Id) cpu_s=$([math]::Round($_.CPU,1)) rss=$([math]::Round($_.WorkingSet64/1MB,1))MB" }; Start-Sleep 3 }
tasklist /FI "IMAGENAME eq python.exe" /FO CSV
```

The k6 fallback (`load.py` as a raw stress generator) if the container can't
reach the host:
```bash
../.venv/Scripts/python.exe load.py --url http://127.0.0.1:8809 --duration 30 --concurrency 50
```

---

## Paced latency reports — see `latency_report.md`

Per-flow latency percentiles (paced, serial, under the rate limiter) are
produced by `copilot/scripts/report_latency.py` (`--mode {mock,live}`; usage in
`../SLOS.md` §4) and land here as `latency_report.md` / `latency_report.json`.
That harness is a **measurement** tool; this file remains the
**saturation/throughput** record (k6 + `load.py` stress runs above).

---

## METHOD / caveats

- **Mock isolates app+DB from the LLM.** The 8809 server stubs the LLM call, so
  its latency/throughput reflect FastAPI + Authorization Gateway + read-only
  MariaDB only — no token spend, no provider latency. The 8808 real-LLM sample
  is deliberately tiny (10 requests) to bound token cost.
- **Co-located generator (loopback).** The load generator ran on the same host
  as the sidecar (Docker→host and 127.0.0.1). These numbers reflect **server
  compute**, not WAN — no real network RTT, TLS, or ingress is included. Live
  latency to a remote client will be higher.
- **Rate limiter disabled for the saturation runs.** The mock@10 / mock@50 and
  CPU/RSS numbers were taken with `COPILOT_RATE_LIMIT_PER_MIN=1000000`, so every
  request reaches the app+DB path (error rate ~0%). With the default limit
  (30 req/60 s per IP) a single generator cannot drive sustained concurrency —
  bursts are shed as ~3 ms 429s — so a real single-source saturation test
  requires the limit raised (as here) or many source IPs. The limiter's
  shedding behavior itself was verified earlier (see the note under Headline).
- **Container-level baselines to follow.** Container CPU / memory / throughput
  under the deployed topology will additionally be captured from Railway
  `service_metrics` after the live sidecar deploy, to complement these local
  process-level numbers.
```
