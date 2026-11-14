# Latency report — Clinical Co-Pilot sidecar

- **Mode:** `mock` (verified against the /chat response body)
- **Date:** 2026-07-16 14:41:14
- **URL:** http://127.0.0.1:8809
- **Git SHA:** d2a0d38188e7af8db5df04457e9478552ed43b1a
- **Pacing:** serial, min 2.1s between requests (stays under the 30 req/60s per-IP rate limiter; non-2xx are counted but excluded from percentiles)
- **N per flow:** ready=20, snapshot=20, day=20, chat_message=10, chat_chip_care_gaps=10
- **Caveat (topology):** loopback / co-located generator — these numbers are server compute only; no WAN RTT, TLS, or ingress is included.
- **Caveat (server counters):** `/metrics/summary` percentiles come from a sliding window of the most recent ~512 samples per series — they reflect recent traffic (this run, if counters deltas match), not process lifetime.

## Client wall-clock latency per flow (ms)

| flow | n | ok | excluded | p50 | p95 | p99 | min | max | mean | status codes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ready | 20 | 20 | 0 | 2574.8 | 4072.1 | 4136.2 | 1453.4 | 4136.2 | 2759.0 | 200:20 |
| snapshot | 20 | 20 | 0 | 470.3 | 525.1 | 603.4 | 450.9 | 603.4 | 480.9 | 200:20 |
| day | 20 | 20 | 0 | 622.7 | 749.5 | 757.0 | 588.7 | 757.0 | 639.4 | 200:20 |
| chat_message | 10 | 10 | 0 | 455.4 | 473.5 | 473.5 | 437.2 | 473.5 | 455.5 | 200:10 |
| chat_chip_care_gaps | 10 | 10 | 0 | 521.6 | 627.6 | 627.6 | 502.6 | 627.6 | 535.0 | 200:10 |

## Server-side `/metrics/summary` (post-run scrape)

- **turns:** count 60, p50 21.3ms, p95 195.2ms, avg 74.4ms, errors 0, degraded 0

### Graph nodes

| node | count | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- |
| answerer | 20 | 13.8 | 16.1 |
| critic | 20 | 0.2 | 0.3 |
| evidence_retriever | 10 | 3.5 | 21.1 |

### Endpoints (Δ count = attribution check: after − before scrape)

| endpoint | count | Δ this run | p50 (ms) | p95 (ms) | 4xx | 5xx |
| --- | --- | --- | --- | --- | --- | --- |
| GET /day | 20 | +20 | 621.6 | 748.3 | 0 | 0 |
| GET /metrics/summary | 1 | +1 | 67.2 | 67.2 | 0 | 0 |
| GET /ready | 20 | +20 | 2647.2 | 4069.6 | 0 | 0 |
| GET /snapshot | 20 | +20 | 469.7 | 523.7 | 0 | 0 |
| POST /chat | 20 | +20 | 501.3 | 556.5 | 0 | 0 |

## Bottleneck analysis (computed from this run)

- Graph-node p95 ranking (server-side): evidence_retriever 21.1ms, answerer 16.1ms, critic 0.3ms
- Bottleneck: evidence_retriever p95 21.1ms is 11% of turn p95 195.2ms -> a non-LLM node dominates the turn.
- POST /chat wall-clock p95 473.5ms vs turns.p95 195.2ms -> ~+278.3ms outside the graph (middleware/serialization/HTTP; a negative gap reflects the sliding-window scope of the server counters).
- /snapshot (no-LLM, app+DB only) p95 525.1ms vs /chat p95 473.5ms -> ~-51.7ms is the LLM/graph share of a chat turn.
- Saturation/throughput is NOT re-measured here — see copilot/load/RESULTS.md (k6 + load.py record: single-worker plateau ~4 req/s).
