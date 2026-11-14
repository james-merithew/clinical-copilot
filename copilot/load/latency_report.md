# Latency report — Clinical Co-Pilot sidecar

- **Mode:** `live` (verified against the /chat response body)
- **Date:** 2026-07-16 14:36:07
- **URL:** http://127.0.0.1:8811
- **Git SHA:** d2a0d38188e7af8db5df04457e9478552ed43b1a
- **Pacing:** serial, min 2.1s between requests (stays under the 30 req/60s per-IP rate limiter; non-2xx are counted but excluded from percentiles)
- **N per flow:** ready=20, snapshot=20, day=20, chat_message=10, chat_chip_care_gaps=10, ingest=3
- **Caveat (topology):** loopback / co-located generator — these numbers are server compute only; no WAN RTT, TLS, or ingress is included.
- **Caveat (server counters):** `/metrics/summary` percentiles come from a sliding window of the most recent ~512 samples per series — they reflect recent traffic (this run, if counters deltas match), not process lifetime.

## Client wall-clock latency per flow (ms)

| flow | n | ok | excluded | p50 | p95 | p99 | min | max | mean | status codes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ready | 20 | 20 | 0 | 1514.0 | 1716.6 | 1716.9 | 1298.0 | 1716.9 | 1519.2 | 200:20 |
| snapshot | 20 | 20 | 0 | 720.0 | 841.6 | 1186.0 | 578.5 | 1186.0 | 726.0 | 200:20 |
| day | 20 | 20 | 0 | 1023.3 | 1326.5 | 1336.0 | 810.4 | 1336.0 | 1043.8 | 200:20 |
| chat_message | 10 | 10 | 0 | 24821.0 | 38792.3 | 38792.3 | 20905.7 | 38792.3 | 26593.9 | 200:10 |
| chat_chip_care_gaps | 10 | 10 | 0 | 28316.2 | 38588.4 | 38588.4 | 21603.5 | 38588.4 | 27814.3 | 200:10 |
| ingest | 3 | 3 | 0 | 530.7 | 10123.9 | 10123.9 | 500.8 | 10123.9 | 3718.4 | 200:3 |

## Server-side `/metrics/summary` (post-run scrape)

- **turns:** count 64, p50 283.4ms, p95 31613.7ms, avg 8567.7ms, errors 10, degraded 16

### Graph nodes

| node | count | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- |
| answerer | 20 | 11028.5 | 21966.1 |
| critic | 20 | 0.1 | 0.3 |
| intake_extractor | 20 | 11321.6 | 15043.9 |
| supervisor | 20 | 3536.2 | 4657.5 |

### Endpoints (Δ count = attribution check: after − before scrape)

| endpoint | count | Δ this run | p50 (ms) | p95 (ms) | 4xx | 5xx |
| --- | --- | --- | --- | --- | --- | --- |
| GET / | 2 | +0 | 1.2 | 3.8 | 0 | 0 |
| GET /day | 20 | +20 | 1021.9 | 1323.7 | 0 | 0 |
| GET /dev/token | 2 | +0 | 1.4 | 120.5 | 0 | 0 |
| GET /metrics/summary | 1 | +1 | 2.3 | 2.3 | 0 | 0 |
| GET /ready | 21 | +20 | 1550.7 | 1715.6 | 0 | 0 |
| GET /snapshot | 21 | +20 | 730.6 | 1184.5 | 0 | 0 |
| GET <unmatched> | 2 | +0 | 0.4 | 0.5 | 2 | 0 |
| HEAD / | 1 | +0 | 0.7 | 0.7 | 1 | 0 |
| POST /chat | 20 | +20 | 27159.2 | 38586.4 | 0 | 0 |
| POST /ingest | 3 | +3 | 528.9 | 10121.5 | 0 | 0 |

## Bottleneck analysis (computed from this run)

- Graph-node p95 ranking (server-side): answerer 21966.1ms, intake_extractor 15043.9ms, supervisor 4657.5ms, critic 0.3ms
- Bottleneck: answerer p95 21966.1ms is 69% of turn p95 31613.7ms -> the LLM dominates the turn.
- POST /chat wall-clock p95 38792.3ms vs turns.p95 31613.7ms -> ~+7178.6ms outside the graph (middleware/serialization/HTTP; a negative gap reflects the sliding-window scope of the server counters).
- /snapshot (no-LLM, app+DB only) p95 841.6ms vs /chat p95 38792.3ms -> ~37950.7ms is the LLM/graph share of a chat turn.
- Saturation/throughput is NOT re-measured here — see copilot/load/RESULTS.md (k6 + load.py record: single-worker plateau ~4 req/s).
