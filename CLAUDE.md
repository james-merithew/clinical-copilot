# CLAUDE.md — Clinical Co-Pilot (AgentForge)

Standing instructions for Claude Code. Read this first every session. The companion docs are the source of truth: **`USERS.md`** (who + why) → **`W1_ARCHITECTURE.md`** (how) → **`AUDIT.md`** (the system we build on). When in doubt, defer to those and to this file.

## What this project is
An AI **Clinical Co-Pilot** embedded in **OpenEMR**. This repo **is a fork of OpenEMR** (PHP + MariaDB). We are **not** building a separate app — the Co-Pilot is a **thin, read-only integration** that runs beside OpenEMR. OpenEMR remains the system of record; the AI layer only surfaces context safely.

**The standard:** build what you could defend to a hospital CTO deciding whether to put it in front of real physicians. Not a demo — a trustworthy system.

**The user (from `USERS.md`):** an outpatient internal-medicine primary-care physician with a full schedule and **~90 seconds between rooms** to understand what changed for the next patient. Every capability must trace to a use case in `USERS.md`. If it doesn't, don't build it.

## Non-negotiable invariants
These are the rules the whole design depends on. Never violate them, even if asked to "just for now."

1. **The LLM is an untrusted planner.** It proposes tool calls only. It has **no database handle** and **no authorization authority**.
2. **Authorization is deterministic and two-layer.** **Delegate the ACL/role decision** to OpenEMR's own gacl (`AclMain::aclCheckCore`) — never re-implement the ACL engine. But gacl is section/category-based, **not per-patient by default**, so the Authorization Gateway **composes the per-patient (user↔patient) scope on top** — a scoping layer the core leaves to the caller, not a re-implementation of OpenEMR's permissions. The agent never holds more access than the requesting user. Check user↔patient **before any read**.
3. **Read-only.** The agent never writes, mutates, or places orders. Tools use a **read-only MariaDB user**.
4. **Hard source attribution.** Every factual claim binds to a **real primary key** in the OpenEMR schema. A deterministic **verifier** runs before any response reaches the user; **unattributed claims are quarantined (fail-closed)**, never shown as fact.
5. **Strict schemas are the source of truth.** Every tool input/output is a **Pydantic** contract (Zod on the client payload). Contracts define behavior; the implementation follows them.
6. **Observability from the start.** Every request gets a unique **correlation ID** threaded through every log line, tool call, and LLM interaction. Use **LangSmith** (or equivalent) for traces/metrics/cost.
7. **`/health` and `/ready` are separate.** `/ready` must actually check OpenEMR, the LLM provider, and LangSmith are reachable — never return 200 unconditionally.
8. **PHI safety.** No PHI at rest in the agent tier (stateless). **Never log PHI values** — log correlation IDs and source keys only. LLM calls assume **BAA + zero-data-retention**. **Only demo data**, ever.
9. **Graceful degradation.** A tool failure returns a partial, explicitly-flagged briefing — never a fabricated value. Missing data is surfaced as a gap, never inferred.

## Architecture at a glance
`OpenEMR chart panel → FastAPI sidecar (/chat,/health,/ready, correlation ID) → Authorization Gateway (→ gacl ACL) → single agent + typed tools → read-only data access (MariaDB; OAuth2 FHIR API as alt) → deterministic verifier → structured JSON → panel`

- **Single agent** (not multi-agent) with a tight tool set.
- **Two-phase response:** prefetch on chart-open + parallel tool fan-out + stream first content (~1.5s target); defer deep history to follow-up turns.
- **Interaction:** structured JSON renders **Action Chips** (bound to allowed intents); free text is a fallback.

## Data model notes (from `AUDIT.md`)
- **Medications live in two tables:** `prescriptions` and `lists` (`type='medication'`) — they can diverge; `active` can be stale. Pick the authoritative row and attribute to its PK.
- Problems/allergies: `lists` (`type='medical_problem'`/`'allergy'`). Labs: `procedure_result` (inconsistent units/ranges). Encounters: `form_encounter` (free-text `reason`). Coded dx: `billing`.
- Treat missing/duplicate/stale data as expected — that's why attribution + "surface the gap" exist.

## Repo layout
- **Docs at repo root:** `W1_ARCHITECTURE.md`, `USERS.md`, `AUDIT.md`, `CLAUDE.md`.
- **Co-Pilot code goes in a subfolder** (e.g. `copilot/`), kept clean of the OpenEMR PHP core. Integrate the panel into OpenEMR's `interface/`; don't scatter agent logic through the monolith.
- Tests live next to the code they cover.

## Deployment (Railway)
One **Railway project**, matching OpenEMR's Docker-Compose shape:
- **OpenEMR web** (Apache/PHP, Docker) — **public** (the only public service).
- **MariaDB** — **private**.
- **Co-Pilot sidecar** (FastAPI) — **private**.
- **LLM provider + LangSmith** — **external**.
- **Render** is the fallback. **Week-1 phasing:** deploy the OpenEMR fork first (get a live public URL), then add the sidecar into the same topology.

To let me drive deploys, the **Railway MCP server** should be installed (see setup below); then ask me to `railway link`, deploy, and read logs.

## How to work in this repo
- **Test-driven.** Write failing tests (unit + boundary/invariant cases) **before** implementation; loop until green. Every eval case must exercise a boundary (missing/empty data), an invariant (claims cite a source), or a regression risk — document the failure mode each guards against.
- **Small, reviewable commits** with clear messages. **Ask before** anything destructive (history rewrite, force-push, deleting files) or before pushing to the remote.
- **Never commit secrets.** API keys and DB creds go in Railway private env / a local `.env` that is git-ignored — never in code, logs, or commits.
- Prefer the **existing OpenEMR mechanisms** (ACL, session/OAuth, FHIR) over reinventing them.

## Current status & next tasks
Part-1 docs are done. Progress:
1. ✅ **Run OpenEMR locally** with demo data (repo Docker setup) — done; steps in `RUN_LOCAL.md` / README.
2. ✅ **Deploy the fork to Railway** — done; public URL live (`https://openemr-production-31ad.up.railway.app`).
3. ✅ **Run the `AUDIT.md` confirm-live checklist** against the running instance — done; real numbers filled in (slow queries, med-divergence counts, default creds, TLS).
4. ⏳ **Next: scaffold/build the FastAPI sidecar** per `W1_ARCHITECTURE.md` (contracts first) — the current open task.

## Setup note (one-time)
Install the Railway integration so I can deploy and read logs. The old
`@railway/mcp-server` npm package is **deprecated** — the MCP server is now
bundled into the Railway CLI. Install the CLI (needs Node/npm), then the MCP
server is `railway mcp`:
```
npm install -g @railway/cli
railway login            # browser auth — that login is yours to do
```
The MCP server is registered in the repo's checked-in `.mcp.json` as
`{ "command": "railway", "args": ["mcp"] }`, so it loads on the next Claude
Code start (once `railway login` has stored your credentials). Then ask me to
`railway link`, deploy, and read logs.

## Durable planning memory (read before any work)
Before starting work in a session, read **`docs/PRD.md`** (what/why),
**`docs/TECH_SPEC.md`** (how), and **`docs/DECISIONS.md`** (why things are the
way they are). Sub-agents doing design or build work must read them too — cite
them instead of re-deriving context.

**Append every non-obvious decision to `docs/DECISIONS.md`** (what was decided,
why, the alternative rejected), newest at the top. This is the project's memory:
it is what lets the next session and every sub-agent start warm instead of
re-litigating settled choices. Run `/bootstrap` to (re)generate the doc set for
a new project or a major new phase.
