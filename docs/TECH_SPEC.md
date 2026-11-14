# TECH_SPEC — Clinical Co-Pilot

> Durable "how" doc. Auto-loaded via CLAUDE.md. The authoritative deep dive is
> [W2_ARCHITECTURE.md](../W2_ARCHITECTURE.md); this is the fast index a session
> or sub-agent reads before touching code.

## Stack
- **OpenEMR** (PHP + MariaDB) — the fork we build on; system of record.
- **Sidecar:** FastAPI (Python 3.12) in `copilot/`, kept out of the PHP core.
- **DB access:** read-only `copilot_ro`; ingestion writes via INSERT-only
  `copilot_ingest` (allowlisted tables only).
- **LLM:** Anthropic; hybrid RAG uses Cohere (embed + rerank) with keyless
  TF-IDF / Claude-listwise / Noop fallbacks.
- **Deploy:** Railway (project `openemr-copilot`) — `openemr`, `mariadb`,
  `copilot`, `copilot-seeder` services. See `copilot/deploy/RUNBOOK.md`.

## Shape
`OpenEMR chart panel → FastAPI sidecar (/chat,/health,/ready, correlation ID) →
Authorization Gateway (gacl + per-patient scope) → supervisor + workers +
critic + typed tools → read-only MariaDB → deterministic verifier → JSON → panel`

## Contracts (the source of truth)
- `copilot/app/contracts.py` — `AgentResponse`, the `AnySourceRef` discriminated
  union (`SourceRef | GuidelineRef | DocumentRef`), graph node/route enums,
  `CriticReport`.
- `copilot/app/ingest/schemas.py` — `LabReportExtraction`, `IntakeFormExtraction`,
  `MedicationListExtraction`; raw VLM output never bypasses these.
- OpenAPI: `copilot/openapi.json`, byte-for-byte drift test; regen via
  `copilot/scripts/export_openapi.py` whenever a route/model on a route changes.

## Key modules
- Graph: `copilot/app/graph/` (supervisor, workers, handoff) + `app/critic.py`.
- Verifier (exit authority): `copilot/app/verifier.py`.
- RAG: `copilot/app/rag/` (bm25, providers, index, corpus, rewrite).
- Ingest: `copilot/app/ingest/` (extractor, pdf_text, verify_extraction,
  writer, service, openemr_api).
- Auth: `copilot/app/session_token.py`, `authz.py`.
- Observability: `correlation.py`, `metrics_summary.py`, `observability.py`.

## The gate (definition of "landed")
`copilot/evals/golden/` — 65 boolean-rubric cases, per-category floors,
case-identity manifest + git_sha freshness guard. Run:
`runner.py --mode hermetic --out results.json` → `gate.py`. Enforced by
`.githooks/pre-push` (authoritative) and `.gitlab-ci.yml` (adds pip-audit +
bandit). Never `--update-baseline` to make red green; it refuses below floors.

## Testing tiers (what guards what)
- Unit: schema validators, tool functions, verifier/critic logic.
- Integration: full ingestion→answer path with fixtures + stubbed VLM/LLM
  (hermetic, no keys).
- Golden set: agent behavior (grounding, refusal, citation survival).
- Every test documents the failure mode it guards.

## Latency / cost
Measured via `copilot/scripts/report_latency.py` (mock + live, honesty
cross-check); reports in `copilot/load/`; SLOs in `copilot/SLOS.md`. Turn is
LLM-dominated — per-node model tiers (`COPILOT_SUPERVISOR_MODEL` /
`COPILOT_WORKER_MODEL`) and `max_retries=1` bound the worst case.
