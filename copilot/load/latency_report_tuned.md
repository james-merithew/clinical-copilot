# Latency report — Clinical Co-Pilot sidecar

- **Mode:** `live` (verified against the /chat response body)
- **Date:** 2026-07-16 16:21:01
- **URL:** http://127.0.0.1:8811
- **Git SHA:** b290e2a8efc044ab2cf0efbd422d2ce70deff328
- **Pacing:** serial, min 2.1s between requests (stays under the 30 req/60s per-IP rate limiter; non-2xx are counted but excluded from percentiles)
- **N per flow:** ready=6, snapshot=6, day=6, chat_message=6, chat_chip_care_gaps=6, ingest=3
- **Caveat (topology):** loopback / co-located generator — these numbers are server compute only; no WAN RTT, TLS, or ingress is included.
- **Caveat (server counters):** `/metrics/summary` percentiles come from a sliding window of the most recent ~512 samples per series — they reflect recent traffic (this run, if counters deltas match), not process lifetime.

## Client wall-clock latency per flow (ms)

| flow | n | ok | excluded | p50 | p95 | p99 | min | max | mean | status codes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ready | 6 | 6 | 0 | 6279.5 | 7202.3 | 7202.3 | 3713.2 | 7202.3 | 5861.1 | 200:6 |
| snapshot | 6 | 6 | 0 | 781.2 | 891.7 | 891.7 | 725.6 | 891.7 | 808.4 | 200:6 |
| day | 6 | 6 | 0 | 940.6 | 1079.6 | 1079.6 | 794.0 | 1079.6 | 935.3 | 200:6 |
| chat_message | 6 | 6 | 0 | 49122.3 | 53278.3 | 53278.3 | 24436.6 | 53278.3 | 42745.0 | 200:6 |
| chat_chip_care_gaps | 6 | 6 | 0 | 36659.0 | 52748.0 | 52748.0 | 27077.1 | 52748.0 | 40402.4 | 200:6 |
| ingest | 3 | 3 | 0 | 749.6 | 827.8 | 827.8 | 730.8 | 827.8 | 769.4 | 200:3 |

## Server-side `/metrics/summary` (post-run scrape)

- **turns:** count 28, p50 233.5ms, p95 52013.1ms, avg 17515.3ms, errors 4, degraded 8

### Graph nodes

| node | count | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- |
| answerer | 12 | 46313.8 | 49747.2 |
| critic | 12 | 0.2 | 0.2 |
| supervisor | 12 | 1275.4 | 1872.3 |

### Endpoints (Δ count = attribution check: after − before scrape)

| endpoint | count | Δ this run | p50 (ms) | p95 (ms) | 4xx | 5xx |
| --- | --- | --- | --- | --- | --- | --- |
| GET / | 1 | +0 | 3.9 | 3.9 | 0 | 0 |
| GET /day | 6 | +6 | 938.2 | 1076.6 | 0 | 0 |
| GET /dev/token | 2 | +0 | 1.5 | 121.9 | 0 | 0 |
| GET /metrics/summary | 1 | +1 | 3.5 | 3.5 | 0 | 0 |
| GET /ready | 6 | +6 | 6276.4 | 7200.1 | 0 | 0 |
| GET /snapshot | 7 | +6 | 843.6 | 2498.0 | 0 | 0 |
| GET <unmatched> | 1 | +0 | 0.4 | 0.4 | 1 | 0 |
| HEAD / | 1 | +0 | 1.1 | 1.1 | 1 | 0 |
| POST /chat | 12 | +12 | 49120.6 | 52745.3 | 0 | 0 |
| POST /ingest | 3 | +3 | 747.5 | 825.3 | 0 | 0 |

## Bottleneck analysis (computed from this run)

- Graph-node p95 ranking (server-side): answerer 49747.2ms, supervisor 1872.3ms, critic 0.2ms
- Bottleneck: answerer p95 49747.2ms is 96% of turn p95 52013.1ms -> the LLM dominates the turn.
- POST /chat wall-clock p95 53278.3ms vs turns.p95 52013.1ms -> ~+1265.2ms outside the graph (middleware/serialization/HTTP; a negative gap reflects the sliding-window scope of the server counters).
- /snapshot (no-LLM, app+DB only) p95 891.7ms vs /chat p95 53278.3ms -> ~52386.6ms is the LLM/graph share of a chat turn.
- Saturation/throughput is NOT re-measured here — see copilot/load/RESULTS.md (k6 + load.py record: single-worker plateau ~4 req/s).
