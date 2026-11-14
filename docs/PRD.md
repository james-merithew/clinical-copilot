# PRD — Clinical Co-Pilot (AgentForge)

> Durable planning doc. Auto-loaded via CLAUDE.md at the start of every session
> and by every sub-agent. Keep it current; it is the "what/why," not the "how"
> (the how lives in [TECH_SPEC.md](TECH_SPEC.md), decisions in
> [DECISIONS.md](DECISIONS.md)).

## What
An AI **Clinical Co-Pilot** embedded in **OpenEMR** — a thin, **read-only**
integration that surfaces "what changed for the next patient" to a busy
outpatient primary-care physician. OpenEMR stays the system of record; the AI
layer only reads and attributes.

## Why / user
Outpatient internal-medicine PCP, full schedule, **~90 seconds between rooms**.
Every capability must trace to that use case (see `USERS.md`). If it doesn't
serve the 90-second briefing, it doesn't get built.

## Core capabilities (Week 2, as-built)
- Grounded briefing: what changed since last visit, can't-miss items — every
  claim bound to a real primary key.
- Document ingestion: lab PDF, intake form, **medication list** — vision
  extraction with per-field citations, fail-closed against the text layer.
- Hybrid RAG + rerank over an office-guideline corpus; guideline evidence kept
  separate from patient facts.
- Supervisor + 2 workers + a deterministic critic node; every handoff logged.
- Lab trend chart from extracted observations.

## Non-negotiables (invariants — see CLAUDE.md for the full list)
1. LLM is an untrusted planner: proposes tool calls only, no DB handle, no auth.
2. Deterministic two-layer authz (OpenEMR gacl + per-patient scope).
3. Read-only agent tier.
4. Hard source attribution; a deterministic verifier quarantines unattributed
   claims (fail-closed).
5. Strict schemas (Pydantic/Zod) are the source of truth.
6. Correlation ID threads every request.
7. `/health` and `/ready` separate; `/ready` checks real dependencies.
8. No PHI at rest in the agent tier; never log PHI values; demo data only.
9. Graceful degradation — partial + flagged, never fabricated.

## Definition of done (per deliverable)
A ticket lands only when the **eval gate passes** (lint → tests → golden gate,
per-category floors, PR-blocking hook). A green reviewer agent is not "done";
the deterministic gate is the authority.

## Out of scope (deliberately not built)
See TECH_SPEC / W2_ARCHITECTURE §13 — OCR for scans, ColQwen2/multi-vector,
LangGraph, a 3rd document type beyond medication_list, and any capability with
no matching use case in `USERS.md`.
