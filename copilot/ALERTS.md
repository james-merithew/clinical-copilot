# ALERTS.md — Clinical Co-Pilot, Week 2 operational alerts

Six alerts, each with a **threshold**, a **signal source** (the exact
`/metrics/summary` field or structured-log event name to watch), and an
**expected response action**. The design principle behind all of them (CLAUDE.md
invariant 9): a degraded dependency produces a *partial, explicitly-flagged*
result or a clean refusal — **never a fabricated value** — so an alert is an
*ops* signal to investigate, and firing an alert never means the physician was
shown something false.

Signals come from two PHI-free surfaces:

- **`GET /metrics/summary`** — in-process counters/rates and p50/p95, rendered on
  `/dashboard`. Keys are route templates and enums; no PHI (§ hardening: a
  404 to an arbitrary path buckets under `<unmatched>`, never the raw path).
- **Structured logs** — JSON lines keyed by `correlation_id`, allowlisted keys
  only (`app/correlation.py`). Every event below is greppable by its `msg`.

All thresholds assume the sidecar is deployed private (CLAUDE.md), so these are
internal ops alerts, not user-facing.

---

## 1. Extraction failure rate > 20%

| | |
| --- | --- |
| **Threshold** | field-level verification pass rate < 0.80 (i.e. >20% of extracted fields fail text-layer verification) over a rolling window |
| **Signal source** | `/metrics/summary` → `ingestion.field_pass_rate` (= `fields_extracted / fields_total`); per-field log event `extract_field_outcome` with `status=failed_verification` |
| **Expected response** | This is **fail-closed by design** — those fields were *not* persisted, they were surfaced as gaps, so there is no bad data to clean up. Investigate *why*: (a) VLM/model availability or version drift (check the LangSmith trace by `correlation_id`); (b) fixture/document-layout drift (a new lab format the extraction schema doesn't cover) — if legitimate, extend the extraction schema via a contract change + `MIGRATIONS.md` note, **never** loosen verification; (c) a run of scanned (no-text-layer) PDFs, which correctly produce `needs_review`. Do **not** set `COPILOT_INGEST_ALLOW_UNVERIFIED=true` to make the number go up. |

## 2. RAG retrieval p95 breach

| | |
| --- | --- |
| **Threshold** | guideline-retrieval p95 exceeds its budget (≤300ms in-process, excl. external rerank; the rerank hard timeout is 3s) |
| **Signal source** | `/metrics/summary` → `nodes.evidence_retriever.p95` and `retrieval` block; log events `reranker_fallback` / `rag_dense_unavailable` |
| **Expected response** | Check Cohere reachability/latency. Confirm the **degraded fallback engaged** (BM25-only + `NoopReranker`) rather than the turn hanging — a p95 breach *with* the fallback working is a latency nuisance, not a correctness problem (grounding never depended on rank order). If Cohere is the cause, retrieval is already degrading gracefully; restore the key/network and p95 recovers. If the breach is in-process (index), check the index built at startup and corpus size. |

## 3. Eval regression > 5% / floor breach (gate exit 1)

| | |
| --- | --- |
| **Threshold** | any golden category regresses >5% vs the committed `baseline.json`, **or** drops below its absolute floor (schema/PHI/refusal 1.00, citation 0.97, factual 0.96), **or** a guarded case-id disappears / a category count shrinks |
| **Signal source** | `gate.py` **non-zero exit** is the trigger (the `.githooks/pre-push` hook and `.gitlab-ci.yml` eval job), together with its printed per-category diff table; `/metrics/summary` → `evals` reflects the last run's per-category rates |
| **Expected response** | **Do not push / do not merge.** Read `gate.py`'s per-category diff table to see which rubric fell and by how much. Fix the regression in code and re-run the hermetic runner. **Never** run `--update-baseline` to make red green — it refuses below the floors anyway, and the freshness guard refuses a stale/hand-edited results file. If the change is a legitimate re-scope (cases genuinely added/removed), update the baseline deliberately in its own reviewable commit. |

## 4. Retrieval degraded (Cohere fallback engaged)

| | |
| --- | --- |
| **Threshold** | any turn served on the BM25-only / `NoopReranker` fallback (degraded retrieval), i.e. `retrieval.degraded > 0` in a window |
| **Signal source** | `/metrics/summary` → `retrieval.degraded` counter; per-turn `AgentResponse.degraded=true` (allowlist log key `retrieval_degraded`); log events `rag_dense_unavailable` (dense embed dropped → BM25-only) / `reranker_fallback` (rerank → Noop) |
| **Expected response** | This is a **supported degraded mode**, not urgent — answers are still grounded, just retrieved sparse-only. It is surfaced here (metrics + per-turn `degraded` flag) rather than by flipping `/ready`, which stays a fast liveness probe (W2 §9.7). Check the Cohere key/network; when restored, the counter stops incrementing and turns return to non-degraded. Escalate only if degraded is *persistent* (Cohere outage) and retrieval quality visibly suffers. |

## 5. Ingestion errors

| | |
| --- | --- |
| **Threshold** | any `POST /ingest` ending in `status=failed` (upload/store/persist error) — alert on rate > 0 |
| **Signal source** | `/metrics/summary` → `ingestion` (`failed` count) and `endpoints["POST /ingest"].errors_*`; log events `ingest_failed` (with `phase`/`error` type) and `openemr_api_error` |
| **Expected response** | Ingestion writes are **transactional** — a failed run persisted nothing (no partial chains). Triage by `phase`: **store** → check `/ready` `openemr_api`; if OAuth (`oauth_keys`) is the cause, follow the runbook to register the client + trigger key generation, or enable `COPILOT_INGEST_DB_DOC_FALLBACK` for the demo; re-upload (dedup makes it safe). **persist** → a DB/data issue (e.g. a date/constraint problem) — inspect by `correlation_id`; the §14 hardening (ISO date normalization, patient-scoped document resolution, autocommit id-recovery) closed the known MariaDB-reality failure modes. |

## 6. 5xx rate

| | |
| --- | --- |
| **Threshold** | server-error rate above ~0 in steady state (any sustained `errors_5xx`) |
| **Signal source** | `/metrics/summary` → `endpoints[*].errors_5xx` (recorded even for **unhandled** crashes — the §14 fix records the 5xx and still emits `x-correlation-id`, so the dashboard no longer shows a healthy service during an outage); log event `unhandled_error` (with `path` + exception `error` **type** only, never the message) |
| **Expected response** | Pull the `correlation_id` from the response header or the `unhandled_error` log line and trace it. A spike on `POST /chat` with `TURNS` not incrementing points at an early failure (authz/DB before the turn observer exists) — check `/ready` `database`. The exception handler is PHI-safe (logs the exception *type*, not its message/DB rows), so start from the type + the failing route. |

## 7. Auth: token-verify failures / signing key unavailable

| | |
| --- | --- |
| **Threshold** | a sustained rate of **401**s on token-protected endpoints (`/chat`, `/ingest`, `/snapshot`, `/day`, `/documents/*`), **or** any `signing_key_read_failed` / `signing_key_table_absent` log event in production |
| **Signal source** | log event `auth_reject` (`reason` enum + `path`; the token itself is **never** logged); `signing_key_read_failed` (with `error` type) and `signing_key_table_absent` warnings from `app/session_token.py`; `/metrics/summary` → `endpoints[*].errors_4xx` (a 401 buckets here) |
| **Expected response** | This is **fail-closed by design** — a request that can't be authenticated is refused, never served. Triage the cause: (a) **signing-key outage** — a `signing_key_read_failed`/`signing_key_table_absent` in prod means the `copilot_signing_key` row is missing or the DB is unreachable, so *every* token fails to verify; re-run the seeder (the row is `INSERT IGNORE`, stable) and confirm `copilot_ro` can `SELECT` it. Note `/ready` does **not** probe the signing key, so a key outage shows up here as a 401 spike, not a `/ready` flip. (b) **expired tokens** — a broad low-level 401 rate is normal (900s TTL; the panel re-mints on the next chart-open); only a *step change* is a signal. (c) **`signing_key_table_absent` with `using_dev_fallback=true` in production** is a misconfiguration — `COPILOT_AUTH_DEV_MODE` must be **false** in prod; a prod sidecar should never take the dev-key fallback. Never "fix" a 401 spike by enabling dev mode or loosening verification. |

---

## Alert-to-invariant map

| Alert | Guards which invariant / promise |
| --- | --- |
| 1 Extraction failure rate | #4 hard source attribution / #9 fail-closed (never persist unverified) |
| 2 Retrieval p95 | #9 graceful degradation (fallback engaged, not hung) |
| 3 Eval regression | #4 attribution + the whole safety-gate discipline |
| 4 Retrieval degraded | #9 degradation is reported, never silent |
| 5 Ingestion errors | #3 read-only agent / transactional writes; #9 partial-flagged |
| 6 5xx rate | #6 observability (every failure traceable by correlation id) |
| 7 Auth token-verify / signing key | #2 deterministic authorization (fail-closed) / #8 PHI safety (token never logged) |
