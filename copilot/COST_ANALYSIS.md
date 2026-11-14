# COST_ANALYSIS.md — Clinical Co-Pilot

AI + infrastructure cost model for the Clinical Co-Pilot sidecar. Covers **actual
development spend to date** and **projected production cost at 100 / 1K / 10K / 100K
users**, where "users" = practicing physicians. This is a bottom-up model of the
real request path (the 2-call tool loop in `app/agent.py`), **not** a naive
`tokens × N` multiplication — each tier states the architectural change it forces.

> **Model under load:** Anthropic `claude-sonnet-5`.
> **List price used:** **$3 / 1M input tokens**, **$15 / 1M output tokens**.
> All figures are engineering estimates for planning, not a quote. Assumptions are
> stated inline so every number can be re-derived.

---

## 1. Per-turn token cost (the atomic unit)

One physician "turn" (a briefing, a follow-up question, or a chip tap) runs the
agent tool loop in `app/agent.py`: typically **~2 LLM calls** — one or more tool
fan-out calls, then the terminal `submit_briefing` call. Observed usage from a real
briefing turn against seeded demo data:

| Component | Tokens | Rate | Cost |
|---|---|---|---|
| Input (both LLM calls: system prompt + tool schemas + evidence + history + user text) | ~4,000 | $3 / 1M | **$0.01200** |
| Output (tool-use JSON + final cited claims) | ~825 | $15 / 1M | **$0.01238** |
| **Per-turn total — uncached** | | | **≈ $0.0244** |

### Prompt caching (the static prefix)

The **system prompt + the full tool-schema block + the `submit_briefing` schema**
are byte-identical on every turn (see `SYSTEM_PROMPT`, `TOOL_SCHEMAS`, and
`SUBMIT_TOOL` in `app/agent.py`). That static prefix is ~2,400 of the ~4,000 input
tokens. With Anthropic prompt caching, cache **reads** bill at ~10% of the input
rate, so the effective input per turn drops to roughly:

```
effective input ≈ 1,600 dynamic tok + (2,400 static tok × 0.10)
               ≈ 1,600 + 240 ≈ 1,840 tok   → $0.00552
```

That is a **~2.2× reduction on input** and a **~26% reduction on the blended
per-turn cost** (output is uncacheable — it dominates once input is cached):

| Per-turn total — **prompt-cached** | ~1,840 in + 825 out | | **≈ $0.0179** |

We use **~$0.024/turn uncached** and **~$0.018/turn cached** below. Cache-write
overhead (1.25× on the first miss) is amortized to near-zero at steady request
volume and is ignored beyond tier 1.

### Turns per physician per month

- ~20 patients/day × ~2.5 turns/patient (open-chart briefing + 1–2 follow-ups/chips) ≈ **~50 turns/physician/day**
- × ~20 workdays/month ≈ **~1,000 turns/physician/month**

So LLM spend is **~1,000 × $0.018 ≈ $18/physician/month** at steady state with
prompt caching (~$24 uncached).

---

## 2. Actual development spend (to date)

The build used a **handful of live `claude-sonnet-5` turns** — live `/chat` smoke
tests, a small LLM-in-the-loop eval sample, and demo rehearsal. Most development
(unit tests, tool/verifier/authz logic, load baselines) ran under
`COPILOT_MOCK_LLM=true`, which bypasses Anthropic entirely and costs **$0** in
tokens.

- Estimated live turns during the build: **~50–150** at ~$0.024/turn
- **Estimated dev LLM spend: ~$1–4 total** (order of a few cents to low single-digit dollars).

> **This is an estimate**, not a metered invoice. It is small because the mock-LLM
> path carried the bulk of test iterations; only end-to-end validation used real
> tokens. Infra dev cost was the existing Railway project (already running for the
> OpenEMR fork), so marginal dev infra spend was effectively $0.

---

## 3. Production projection — 100 / 1K / 10K / 100K physicians

Each tier lists LLM token cost, the supporting infra (FastAPI sidecar CPU/mem on
Railway, the MariaDB read path, LangSmith observability ingest), and **the one
architectural change the tier forces**. Monthly turns = physicians × 1,000.

| Tier (physicians) | Turns / mo | LLM $/mo | Infra $/mo | Observability $/mo | **Total $/mo** | **$/physician/mo** |
|---|---|---|---|---|---|---|
| **100** | 100 K | ~$1,800 | ~$50 | ~$50 | **~$1,900** | **~$19** |
| **1 K** | 1 M | ~$14,400 | ~$150 | ~$450 | **~$15,000** | **~$15** |
| **10 K** | 10 M | ~$135,000 | ~$2,500 | ~$1,500 | **~$139,000** | **~$14** |
| **100 K** (self-host) | 100 M | ~$250,000* | ~$60,000 | ~$8,000 | **~$318,000** | **~$3.2** |
| **100 K** (stay on API) | 100 M | ~$1,800,000 | ~$40,000 | ~$8,000 | **~$1,850,000** | **~$18.5** |

\* Self-hosted open-model inference cost (GPU fleet + ops), replacing per-token API
billing. The gap between the two 100K rows is the entire reason the tier forces a
platform change — see below.

### Assumptions baked into the table

- LLM $/mo uses the **prompt-cached** ~$0.018/turn, with an added **response-cache
  discount at 1K+** (see below) that trims billable turns ~20–25%.
- Infra $/mo = Railway compute for the sidecar fleet + DB read path. The MariaDB
  primary already exists for OpenEMR (system of record); the Co-Pilot adds only a
  **read** load, so its marginal DB cost is a read replica at higher tiers, not a
  new primary.
- Observability $/mo = LangSmith trace ingest (one trace per turn, nested spans),
  with **sampling** kicking in at 10K+ to keep ingest sub-linear.

### Tier 1 — 100 physicians: single sidecar + prompt caching

- One FastAPI sidecar instance (Railway), pooled read-only connections to the
  existing `mariadb`. No replica needed.
- **Prompt caching on** (the static system + tool prefix) is the only optimization
  required — it is free to enable and delivers the ~26% blended saving above.
- LangSmith full-fidelity tracing (100 K traces/mo is comfortably in a standard
  plan tier).
- **Bottleneck:** none structural; cost is ~99% LLM tokens.

### Tier 2 — 1K physicians: response caching + connection pooling

- **Response caching for the chart-open prefetch.** The two-phase design
  (`W1_ARCHITECTURE.md`) fires a briefing on chart-open. Many opens re-briefe an
  **unchanged** chart; cache the verified `AgentResponse` keyed by
  `(pid, chart-version/last-write-timestamp)` and serve it with **zero LLM calls**
  until the chart changes. Realistically removes **~20–25%** of billable turns.
- **DB connection pooling** across sidecar replicas (2–3 instances behind a load
  balancer) so the read path doesn't exhaust MariaDB connections.
- Observability moves to a paid LangSmith tier; still full-fidelity.
- **Bottleneck:** LLM tokens still dominate (~96% of spend). Everything else is
  latency/robustness, not cost.

### Tier 3 — 10K physicians: horizontal autoscaling + DB read replica + batch tracing

- **Horizontal autoscaling** of the stateless sidecar (it holds no PHI at rest, so
  it scales cleanly — CLAUDE.md invariant 8).
- **Dedicated MariaDB read replica** for the Co-Pilot so its read fan-out never
  contends with OpenEMR's transactional write load on the primary.
- **Batch / sampled tracing:** stop shipping 100% of traces to LangSmith. Sample
  ~10–20% for the dashboards + always-capture on error/quarantine/degraded, keeping
  ingest cost sub-linear while preserving the alerting signal.
- **Bottleneck:** still LLM tokens (~97%), but infra now needs real ops.

### Tier 4 — 100K physicians: self-host an open model (per-token API cost dominates)

At 100 M turns/month, staying on the metered API is **~$1.8 M/mo** and swamps
everything else. This is the tier where the economics **force a platform change**:

- **Self-host an open-weight model** (a Llama/Qwen-class instruct model, quantized)
  on an owned/reserved GPU fleet, fronted by a high-throughput inference server
  (vLLM/TGI-style continuous batching). Fixed GPU + ops cost (~$250K/mo modeled)
  replaces variable per-token billing — a **~$1.5 M/mo saving** and the drop from
  ~$18.5 to ~$3.2 per physician. (Trade-off: the clinical verifier + constraint
  engine + attribution invariant become **more** important, not less, because an
  open model needs the deterministic guardrails to hit the same trust bar. The
  strict-contract + Verifier architecture is what makes a model swap safe.)
- **Regional sharding:** run the sidecar + read replica + inference per region for
  data residency and latency; route by physician's org.
- **Dedicated cache tier:** promote the response cache to a managed distributed
  cache (Redis-class) shared across regions/replicas instead of per-instance memory.
- **Bottleneck flips:** compute/GPU capacity and cache hit-rate now drive cost, not
  the per-token API rate.

---

## 4. Sensitivity — what moves the number most

1. **Output tokens.** Output is uncacheable and 5× the input rate; it is ~50–70% of
   the cached per-turn cost. Tightening the briefing (fewer, denser claims) is the
   single highest-leverage cost lever after caching.
2. **Turns per physician per day.** The 50/day estimate is load-bearing. Aggressive
   response caching of the prefetch could cut effective billable turns toward ~35–40.
3. **Cache hit-rate on chart-open.** Directly scales the tier-2+ discount; a chart
   that changes between every open (busy inpatient service) caches poorly — this is an
   **outpatient** panel (USERS.md), which caches well.
4. **Model choice.** Dropping to a smaller Anthropic model or the self-host path is
   the only step-change; within a model, caching + output discipline are the levers.

## 5. Bottom line

- **Dev spend:** ~$1–4 (estimate) — mock-LLM path kept live-token use tiny.
- **Unit economics:** ~$0.018/turn cached → **~$18/physician/month** in LLM cost,
  roughly flat per-physician from 100→10K users as infra stays a rounding error.
- **The forced changes:** prompt cache (100) → response cache + pooling (1K) →
  autoscale + read replica + batch tracing (10K) → **self-host open model** +
  regional sharding + dedicated cache (100K), where per-token API cost finally
  dominates and a fixed-cost inference platform wins by ~5–6×.

---

## 6. Week 2 — multimodal cost (ingestion + RAG + supervisor graph)

Week 2 adds three cost surfaces on top of the Week 1 turn: **document ingestion**
(a vision call, off the critical path), **guideline retrieval** (Cohere embed +
rerank inside a chat turn), and the **supervisor + workers** graph (more LLM
calls per `/chat` turn than the Week 1 flat loop). All figures below are
**engineering estimates**, labelled as such; the same list price applies
(`claude-sonnet-5`: **$3 / 1M input, $15 / 1M output**), plus Cohere for
embed/rerank.

### 6.1 Per-ingest cost (one document) — **estimate**

Ingestion is **one Anthropic vision call** per document (the PDF as a document
content block, forced extraction tool), retried ≤2× only on schema-invalid
output. Everything else on the path (pdfium text-layer verification, the DB
writes, provenance) is deterministic code — **$0 in tokens**.

| Component | Tokens (est.) | Rate | Cost (est.) |
|---|---|---|---|
| Vision input (PDF page image(s) + the forced extraction schema + system) | ~2,500–4,000 | $3 / 1M | **~$0.008–0.012** |
| Output (the filled `LabReportExtraction` / `IntakeFormExtraction` JSON) | ~600–1,200 | $15 / 1M | **~$0.009–0.018** |
| **Per-document total — typical, single vision call** | | | **≈ $0.02–0.03** |

Notes: a **scanned** PDF (no text layer) still costs the one vision call but
persists nothing (`needs_review`), so its marginal *storage/verify* cost is zero.
A **duplicate** upload short-circuits on the `(content_hash, pid)` dedup **before**
the vision call — **$0**. Ingestion is **pre-visit and off the `/chat` critical
path**, so its dollar cost matters but its *latency* does not compete with the
physician's 90 seconds (see `SLOS.md`).

### 6.2 Per-chat cost (Week 2 graph turn) — **estimate**

A Week 2 `/chat` turn can fan out further than the Week 1 2-call loop: the
**supervisor** makes one small routing call, each engaged **worker** runs a
bounded sub-loop, and the **answerer** makes the terminal `submit_briefing` call.
Cohere embed/rerank bill separately (and only when the evidence-retriever runs).

| Component | Calls (est.) | Tokens / call (est.) | Cost (est.) |
|---|---|---|---|
| Supervisor routing (forced 3-tool, small prompt) | 1 | ~1,200 in / ~120 out | **~$0.005** |
| Intake-extractor worker (≤3 iters; only when a document turn) | 0–3 | ~2,000 in / ~400 out | **~$0.00–0.024** |
| Evidence-retriever worker (≤2 iters; only when a guideline turn) | 0–2 | ~1,800 in / ~300 out | **~$0.00–0.014** |
| Answerer (the Week 1 terminal loop, now seeded with worker evidence) | 1–2 | ~4,000 in / ~825 out | **~$0.018–0.024** |
| Cohere embed (query) + rerank (per `search_guidelines`) | per retrieval | — | **~$0.0002–0.001** (Cohere pricing; small) |
| **Per-turn total — typical (one worker engaged)** | | | **≈ $0.03–0.05 uncached** |

- A **Week 1-shaped turn** (no document, no guideline — supervisor routes
  straight to `finalize`/answerer) stays at roughly the Week 1 **~$0.024
  uncached / ~$0.018 cached** — the graph adds only the small supervisor call.
- **Prompt caching** still applies: the answerer's static system + tool-schema
  prefix and the supervisor's fixed 3-tool prompt are byte-identical per turn, so
  the cached blended cost lands well below the uncached range above (the ~26%
  input saving from §1 carries over).
- The dominant Week 2 cost driver is **which workers engage**, not raw token
  size — a "what changed?" turn that pulls in both a document and a guideline is
  the expensive case; a plain follow-up is Week-1-cheap.

### 6.3 p50/p95 methodology

Latency percentiles are captured by the committed measurement harness
**`copilot/scripts/report_latency.py`** (`--mode {mock,live}` required, and the
claimed mode is cross-checked against the observed `/chat` body): paced serial
flows produce client wall-clock p50/p95/p99 per flow plus a before/after scrape
of the in-process `/metrics/summary` counters, which carry `p50`/`p95` per
endpoint **and per graph node** (`turns`, `nodes`, `endpoints`, `ingestion`,
`retrieval`), rendered on `/dashboard`. Reports land in
`copilot/load/latency_report.md` / `.json`; usage lives in `SLOS.md` §4.
Week 1 `/chat` / `/snapshot` appear alongside the Week 2 flows so a shared-path
regression is visible in one place. `SLOS.md` records which rows are measured
versus design targets, and the enforced ceilings (20s turn deadline,
per-dependency timeouts) that bound worst-case cost per turn regardless of
provider latency.

**Measured (2026-07-16, local loopback, committed reports in `copilot/load/`):**
the live pass (`latency_report.md`, n=10 per chat flow) measured `POST /chat`
at **p50 24.8s / p95 38.8s** — far above the 3–5s/8–12s design target — while
the mock pass (`latency_report_mock.md`) puts the app+DB share at just
**~0.46s p50**, confirming the bottleneck analysis: the **answerer's LLM call
is ~69% of turn p95** (server nodes: answerer p95 22.0s, intake_extractor
15.0s, supervisor 4.7s, critic **0.1ms** — the new critic node is free).
Deterministic endpoints hold their class: `/snapshot` p50 0.72s, `/day` 1.02s,
`/labs/trend` 0.43s, `/ingest` p50 0.53s (dedup path) with a 10.1s max on the
first real extraction — inside its 30s off-critical-path budget. The honest
conclusion: the target rows are **missed at current provider latency**; the
bound that holds is the enforced 20s deadline forcing a flagged partial
(observed as `degraded` turns), and the mitigation path is model/latency
tuning, not architecture.

### 6.4 Bottom line (Week 2)

- **Per-document ingest:** ~$0.02–0.03 (one vision call), off the critical path;
  duplicates and pre-vision dedup hits are $0.
- **Per-chat:** Week-1-cheap for a plain turn (~$0.018 cached); ~$0.03–0.05 when a
  worker engages. Cost scales with *worker engagement*, and the deterministic
  verifier/constraint spine is unchanged — so a cheaper or self-hosted model can
  be swapped in behind the same guardrails without loosening the trust bar.

### 6.5 LLM judge panel — eval-only, on-demand call budget (not per-commit)

The 4-lens judge panel (§8.6 of `W2_ARCHITECTURE.md`) is an **eval-quality
instrument that costs $0 in production** — it is never on the `/chat` path and
never in the pre-push/CI gate chain. Its only spend is when a developer runs it
deliberately, and even then it defaults to a **hermetic stub (zero API calls)**;
real judging requires `COPILOT_JUDGE=live` + an Anthropic key.

- **Call arithmetic:** each judged case runs **3 LLM lenses × 2 order-swaps = 6
  forced-tool calls** (the `security` lens is a deterministic PHI scan — 0 API
  calls). A full pass over all 57 golden cases is therefore ≈ **340 live
  calls/run** — which is exactly why it is **on-demand, not per-commit**.
- **The default `--mode ablation` demo** runs a small/sampled set, so the
  submission artifact is **~dozens of calls**, not the full 340. `--sample N`
  bounds it explicitly (`6 × N` calls).
- **Per-call size** is small (two short responses + one lens prompt, ~1–2k in /
  ~100 out), so even a full live run is on the order of **a few dollars**, not a
  recurring cost — a once-in-a-while quality check, not a budget line.

The takeaway: the panel deepens the eval story without moving the production cost
model above — the numbers in §1–§6.4 are unchanged by it.
