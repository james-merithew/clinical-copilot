# Clinical Co-Pilot — an AI layer for OpenEMR (AgentForge MVP)

This repository holds the **Clinical Co-Pilot layer** I built on top of
[OpenEMR](https://open-emr.org): every file I added or changed in my OpenEMR fork
(the `copilot/` service, the OpenEMR module in `interface/modules/custom_modules/oe-module-copilot/`,
the eval suites, the `redteam/` platform, and the architecture and defense docs).
Stock OpenEMR is not included; drop these paths onto an OpenEMR checkout to run it.

The Co-Pilot is a thin, **read-only**, deterministically-authorized AI
integration that surfaces "what changed" for the next patient to a busy
primary-care physician (~90 seconds between rooms). OpenEMR remains the system
of record; the Co-Pilot only reads and attributes.

> The standard we build to: something you could defend to a hospital CTO
> deciding whether to put it in front of real physicians — not a demo.

## 🔗 Deployed app (live)

**https://openemr-production-31ad.up.railway.app** — a live, public deployment
of this OpenEMR fork on Railway (web service public, MariaDB private).

- Login: `admin` / _(strong password, shared out-of-band — not stored in this repo)_
- Deployment topology, redeploy steps, and known gaps: **[DEPLOY.md](DEPLOY.md)**

## 🛡️ Week 3 — Adversarial Security Platform (MVP)

A multi-agent red-team platform ([`redteam/`](redteam)) that continuously attacks **this**
Co-Pilot and proves whether its defenses hold. Four distinct agents —
**Orchestrator → Red Team → Judge → Documentation** — coordinate over versioned JSON-Schema
[contracts](contracts). The Judge's authority is a **deterministic invariant, never an LLM
vote**; confirmed exploits become **versioned regression cases** so a fix that merely changes
behavior can't turn the gate green. Full design in [ARCHITECTURE.md](ARCHITECTURE.md).

**Live attack target (the deployed Co-Pilot sidecar):**
`https://copilot-production-5e86.up.railway.app` — behind the public
[OpenEMR](https://openemr-production-31ad.up.railway.app).

**Run the platform against the LIVE deployed target** (unauth surface needs no key):

```bash
# from the repo root, using the copilot venv interpreter
python -m redteam.live_proof --category unauth_surface     # 1 live attack → confirmed finding → report
python -m redteam.materialize_live_reports                 # ≥3 full vuln reports from live probes → defense/week3/reports/
```

**Run the full hermetic suite + deterministic gate** (zero keys, zero network — the harness proof):

```bash
python -m redteam.runner --target hermetic --out evals/adversarial/results.json
python -m redteam.gate   --results evals/adversarial/results.json --baseline evals/adversarial/baseline.json
```

**Run against a LOCAL Co-Pilot** (start the sidecar on `127.0.0.1:8811` first):

```bash
python -m redteam.runner --target local     # add --category unauth_surface to bound it
```

| Week-3 deliverable | Where |
| --- | --- |
| Threat model (6 categories, OWASP-mapped) | [THREAT_MODEL.md](THREAT_MODEL.md) |
| Multi-agent architecture + diagram + AI-use disclosure | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Attack suite (7 categories, OWASP-mapped, reproducible) | [evals/adversarial/](evals/adversarial) |
| Versioned inter-agent contracts + typed error schema | [contracts/](contracts) |
| Vulnerability reports (live, full-schema) | [defense/week3/reports/](defense/week3/reports) · [live_proof.md](defense/week3/live_proof.md) |
| Triage report (13 findings: crit/high/med/false-pos) | [W3_TRIAGE_REPORT.md](W3_TRIAGE_REPORT.md) |
| Build-vs-configure decision record | [W3_BUILD_VS_CONFIGURE.md](W3_BUILD_VS_CONFIGURE.md) |
| ATO-style evidence packet | [W3_ATO_PACKET.md](W3_ATO_PACKET.md) |
| Platform cost analysis (100/1K/10K/100K test runs) | [W3_COST_ANALYSIS.md](W3_COST_ANALYSIS.md) |
| One-pager | [docs/W3_ONEPAGER.md](docs/W3_ONEPAGER.md) |

## 📦 Week 2 Early Submission (2026-07-16)

**Start here:** log in to the [deployed OpenEMR](https://openemr-production-31ad.up.railway.app) → open **Marcus Delgado (pid 101)** → the **Co-Pilot bubble** sits bottom-right of the chart (the **Co-Pilot menu item** opens the My Day view). The chart icon in the panel's top bar opens the [system dashboard](https://copilot-production-5e86.up.railway.app/dashboard).

Everything below is on `feat/week2-mvp` (merged to `main`; tag `week2-early-submission`):

| Deliverable | Where |
| --- | --- |
| MVP core (2 doc types, supervisor+2 workers, hybrid RAG+rerank, deployed demo) | as graded at MVP; [W2_ARCHITECTURE.md](W2_ARCHITECTURE.md) |
| Grader pointer 1 — `/ready` rubric checks | [`/ready`](https://copilot-production-5e86.up.railway.app/ready) live: `vector_index`, `reranker`, `document_storage` (+ core + ingest trio), `degraded`-not-binary (§9.7). The `vector_index` probe caught a real prod defect (corpus missing from the image) on its first deploy — fixed same day |
| Grader pointer 2 — observability walkthrough in the demo | [copilot/DEMO_SCRIPT.md](copilot/DEMO_SCRIPT.md) Beat 4 + W2 Beat 9 (correlation ID → trace tree → PHI-free logs → `/ready` → dashboard) |
| Critic agent | deterministic 4th graph node ([`app/critic.py`](copilot/app/critic.py), W2 §7.5) — rejects uncited claims to quarantine, strips unsafe order/med suggestions; visible in every turn's handoff trail |
| Third document type | `medication_list` (persists to `lists`, never `prescriptions` — W2 §4.1) |
| Lab trend chart widget | `GET /labs/trend` + inline-SVG chart on the `lab trend` chip; every point cites its `procedure_result` PK |
| Contextual retrieval improvement | deterministic clinical query expansion ([`app/rag/rewrite.py`](copilot/app/rag/rewrite.py)) with regression-locked miss→hit proofs |
| Latency/cost report (actual p50/p95 + bottlenecks) | [`copilot/scripts/report_latency.py`](copilot/scripts/report_latency.py) harness; measured reports in [copilot/load/](copilot/load/); tables in [SLOS.md](copilot/SLOS.md) §4 + [COST_ANALYSIS.md](copilot/COST_ANALYSIS.md) §6.3 (MISS verdicts reported honestly, with per-node model tiers + bounded retries applied and re-measured) |
| Eval gate | **65 cases** (57 + 4 medication-list + 4 critic tripwires), boolean rubrics, hermetic, PR-blocking pre-push hook; to verify, weaken the provenance check in [`copilot/app/verifier.py`](copilot/app/verifier.py) and push — the gate fails with a per-category diff ([recipe](copilot/evals/golden/README.md)) |
| 4-judge LLM panel (extension) | `copilot/evals/golden/panel_report.py --mode ablation` — order-swap debiased, deterministic safety veto, report-only by design |
| Auth | no browser bearer: OpenEMR mints a short-lived token after its own ACL check, bound to user+patient; cross-patient → 403, tampered/expired → 401; patient 120 is the cross-provider denial fixture ([spec](copilot/AUTH_TOKEN_SPEC.md)) |

## 📚 Project documentation

| Doc | What it covers |
| --- | --- |
| **[USERS.md](USERS.md)** | Target user + use cases, and why an agent |
| **[W1_ARCHITECTURE.md](W1_ARCHITECTURE.md)** | Week 1 integration plan, ~500-word summary, tradeoffs |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | Week 3 adversarial security platform architecture (multi-agent red-team) |
| **[THREAT_MODEL.md](THREAT_MODEL.md)** | Week 3 attack-surface threat model (6 categories, OWASP map) |
| **[W2_ARCHITECTURE.md](W2_ARCHITECTURE.md)** | Week 2 multimodal architecture (as-built): ingestion, RAG, supervisor graph, eval gate, §14 adversarial review |
| **[AUDIT.md](AUDIT.md)** | Five-part audit + ~500-word summary + confirm-live numbers from the running fork |
| **[CLAUDE.md](CLAUDE.md)** | Standing project instructions + invariants |
| **[RUN_LOCAL.md](RUN_LOCAL.md)** | Full local run procedure |
| **[DEPLOY.md](DEPLOY.md)** | Railway deployment |
| **[copilot/MIGRATIONS.md](copilot/MIGRATIONS.md)** · **[SLOS.md](copilot/SLOS.md)** · **[ALERTS.md](copilot/ALERTS.md)** | Week1→Week2 schema/contract migrations · SLOs · operational alerts |

## 🚀 Local setup (quick start)

Runs OpenEMR + MariaDB with demo data via the repo's Docker stack. Requires
Docker Desktop. Full detail (ports, credentials, reset, verification) in
**[RUN_LOCAL.md](RUN_LOCAL.md)**.

```bash
cd docker/development-easy-light
docker compose up -d          # first boot pulls images + auto-installs (~5–10 min)
docker compose logs -f openemr    # wait for install to finish; openemr turns "healthy"
```

Then open **http://localhost:8300** and log in as `admin` / `pass`. The stack
seeds OpenEMR's standard demo data (3 sample patients) via `DEMO_MODE=standard`;
phpMyAdmin is at http://localhost:8310 for DB inspection.

## 🏗️ Architecture overview

Full plan and tradeoffs in **[W1_ARCHITECTURE.md](W1_ARCHITECTURE.md)**. In one line:

```
OpenEMR chart panel → FastAPI sidecar (/chat,/health,/ready, correlation ID)
  → Authorization Gateway (→ OpenEMR gacl ACL) → single agent + typed tools
  → read-only MariaDB (OAuth2 FHIR API as alt) → deterministic verifier
  → structured JSON → panel
```

Load-bearing invariants (see [CLAUDE.md](CLAUDE.md)): the LLM is an untrusted
planner with **no DB handle and no authority**; authorization is **two-layer** —
the ACL/role decision is **delegated to OpenEMR's gacl** (never re-implemented)
and the Authorization Gateway **composes the per-patient scope** on top, checked
before every read; the agent is **read-only**; every factual claim binds to a
**real primary key**, and a deterministic **verifier** quarantines unattributed
claims (fail-closed); only demo data, ever.

## 🤖 Clinical Co-Pilot sidecar

The Co-Pilot itself is a **read-only FastAPI sidecar** that runs *beside*
OpenEMR (never inside the PHP core), in [`copilot/`](copilot/). It reads the
OpenEMR MariaDB through a dedicated read-only user, runs a single tool-using
agent, and returns source-attributed briefings through a deterministic verifier.
Observability is via **LangSmith** (tracing, a Monitor dashboard, and 3 alert
rules).

**Deployed / live sidecar:** https://copilot-production-5e86.up.railway.app
(private behind OpenEMR's private network in production — the DB is never public).
Patient endpoints require a **short-lived, OpenEMR-minted session token** (see
[Authentication](#-authentication-week-2) below); `GET /health` and `GET /ready`
are open probes, and the chat UI is served at `/`.

**Run it locally** (alongside the OpenEMR + MariaDB stack above):

```bash
cd copilot
python -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
cp .env.example .env        # fill in DB creds, ANTHROPIC_API_KEY, LANGSMITH_API_KEY (git-ignored)
uvicorn app.main:app --port 8000
```

Then `GET /health` (liveness), `GET /ready` (per-dependency readiness: core
`database` / `llm` / `observability`, plus `vector_index` and `reranker`, plus
`openemr_api` / `ingest_db` / `document_storage` when ingest is enabled — an
ancillary failure reports `degraded` with 200, a core failure is 503 `down`),
and `POST /chat` (free-text or chip turn). The sidecar reads the same local
`docker/development-easy-light` MariaDB the OpenEMR stack uses.

**Dataset.** For realistic breadth the demo/eval data is **~20 Synthea
synthetic patients** (imported via OpenEMR's devtools `import-random-patients`)
plus **2 curated showcase patients** that guarantee the demo beats (a
`prescriptions`↔`lists` med divergence, an A1c trend, and an allergy↔active-med
contraindication the constraint engine fires on).

## 🧭 Week 1 baseline vs Week 2 multimodal

The Co-Pilot ships in two layers. **Week 1** is the grounded text briefing;
**Week 2** adds document ingestion, guideline RAG, and a supervisor graph *on top
of the same verified pipeline* — additively, so every Week 1 flow is unchanged
(see [`copilot/MIGRATIONS.md`](copilot/MIGRATIONS.md)).

| | Week 1 baseline | Week 2 multimodal (adds) |
| --- | --- | --- |
| **Branch** | `feat/copilot-mvp` | **`feat/week2-mvp`** (this work) |
| **Question answered** | "what changed since last visit, what can't I miss?" | + "the answer is in an uploaded PDF" and "what's the *evidence* for the recommendation?" |
| **Capabilities** | single tool-using agent over read-only patient tools; deterministic verifier; Action Chips | + **document ingestion + vision extraction** (`POST /ingest`, `app/ingest/`; doc types: `lab_report`, `intake_form`, `medication_list`); + **per-field citations with bbox click-to-source** (`/documents/{id}/page/{n}.png`); + **hybrid RAG** over an office-guideline corpus (Cohere primary → TF-IDF/Claude fallback, `app/rag/`); + **supervisor + 2 workers** with logged handoffs (`app/graph/`); + **deterministic critic node** (`app/critic.py`) |
| **Endpoints** | `/health` `/ready` `/chat` `/snapshot` `/day` | + `/ingest` `/documents/{id}/meta` `/documents/{id}/page/{n}.png` `/documents/{id}/citations` `/labs/trend` `/metrics/summary` `/dashboard` |
| **Citation contract** | `SourceRef` (one DB PK) | discriminated union `SourceRef` \| `GuidelineRef` \| `DocumentRef` (additive) |
| **Auth** | reusable static browser bearer (`COPILOT_BEARER_SECRET`) | **revoked**; short-lived **OpenEMR-minted session token** (gacl `aclCheckCore` at mint → fragment → sidecar HMAC verify), patient-bound; real OpenEMR ACL + per-patient scope ([spec](copilot/AUTH_TOKEN_SPEC.md)) |
| **DB footprint** | read-only `copilot_ro` on `openemr` | + `copilot` schema (`ingestion_run`/`provenance`/`intake_fact`) + INSERT-only `copilot_ingest` writer + `copilot_signing_key` (token HMAC secret) |
| **Eval gate** | golden set + pre-push hook | 65-case gate, floors schema/PHI/refusal 1.00, citation 0.97, factual 0.96 |
| **Graph shape** | single agent loop | supervisor → workers → answerer → **critic** → verifier: the 4th node is a deterministic pre-verifier reviewer (no LLM) that rejects uncited claims to quarantine and strips order/medication *recommendations* from the patient briefing and Action Chips — the read-only invariant enforced on content, not just tool capability. Guideline-only evidence is exempt (the corpus legitimately says "start a statin"); every turn's handoff trail ends `critique → critic_passed | critic_rejected`. See `W2_ARCHITECTURE.md` §7.5 |
| **Docs** | ARCHITECTURE / AUDIT / USERS | + [`W2_ARCHITECTURE.md`](W2_ARCHITECTURE.md), [`copilot/MIGRATIONS.md`](copilot/MIGRATIONS.md), [`copilot/SLOS.md`](copilot/SLOS.md), [`copilot/ALERTS.md`](copilot/ALERTS.md), [`copilot/COST_ANALYSIS.md`](copilot/COST_ANALYSIS.md), [`copilot/DEMO_SCRIPT.md`](copilot/DEMO_SCRIPT.md) |

### Run the Week 2 flow locally

Runs the sidecar with ingestion + RAG enabled against the local
`docker/development-easy-light` MariaDB. From the repo root:

```bash
# 0. OpenEMR + MariaDB stack up (see quick start above); then seed the copilot schema:
mysql -h127.0.0.1 -P8320 -uroot -p < copilot/deploy/seeder/live_seed.sql   # copilot schema + copilot_ingest user
mysql -h127.0.0.1 -P8320 -uroot -p < copilot/sql/seed_showcase.sql          # showcase patients 101/102

# 1. sidecar venv + deps
cd copilot
python -m venv .venv && source .venv/bin/activate    # Python 3.11+  (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

# 2. .env — Week 2 needs these on top of the Week 1 DB/LLM vars:
#      COHERE_API_KEY=...                 # RAG primary (omit to use the keyless TF-IDF/Claude fallback)
#      COPILOT_INGEST_ENABLED=true        # turn on /ingest + the document viewer
#      COPILOT_INGEST_DB_USER=copilot_ingest
#      COPILOT_INGEST_DB_PASSWORD=...     # must match live_seed.sql's __INGEST_PASS__
#      COPILOT_AUTH_DEV_MODE=true         # LOCAL ONLY — exposes /dev/token so the UI + curl
#                                         # can mint a session token without an OpenEMR login.
#                                         # MUST be false (or unset) in production. See Authentication below.
cp .env.example .env && $EDITOR .env

# 3. run
uvicorn app.main:app --port 8808

# 4. exercise the flow (or use copilot/api-collection/ in Bruno/Postman):
#    patient endpoints need an OpenEMR-minted session token; in dev mode, mint one:
TOKEN=$(curl -s "http://127.0.0.1:8808/dev/token?scope=patient&pid=101" | python -c "import sys,json;print(json.load(sys.stdin)['token'])")
#    upload a lab PDF for pid 101, then ask "what changed for this patient?"
curl -s -X POST http://127.0.0.1:8808/ingest \
  -H "Authorization: Bearer $TOKEN" \
  -F pid=101 -F doc_type=lab_report \
  -F file=@copilot/tests/ingest/fixtures/demo_lab_delgado.pdf
#    med lists work the same way (rows land in `lists`, never `prescriptions`):
#      -F doc_type=medication_list -F file=@copilot/tests/ingest/fixtures/demo_medlist_delgado.pdf
curl -s -X POST http://127.0.0.1:8808/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"pid":101,"message":"what changed for this patient?"}'
```

In production there is **no `/dev/token`** — OpenEMR itself mints the token after
an ACL check when the physician opens a chart (see Authentication below).

The chat UI is at `/`, and the ops dashboard at `/dashboard` (its data loads only with a
valid session token). `/openapi.json` and `/docs` are **disabled by default** in production
(Week-3 attack-surface hardening — set `COPILOT_EXPOSE_DOCS=true` to expose them); the
committed [`copilot/openapi.json`](copilot/openapi.json) is the source of truth, kept in sync
by a drift test. Full walkthrough: [`copilot/DEMO_SCRIPT.md`](copilot/DEMO_SCRIPT.md).

### Environment variables

Config resolves from the environment (Railway private vars / a git-ignored
`copilot/.env`); every field accepts a `COPILOT_`-prefixed name **and** the bare
name Railway injects. Source of truth: [`copilot/app/config.py`](copilot/app/config.py).

**Week 1 base (unchanged):** `COPILOT_DB_HOST` / `_PORT` / `_USER`
(`copilot_ro`) / `_PASSWORD` / `_NAME`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
`COPILOT_DEMO_USER_ID`, `COPILOT_DEFAULT_PID` (101),
`COPILOT_MOCK_LLM`, `COPILOT_RATE_LIMIT_PER_MIN`, `LANGSMITH_API_KEY` /
`_PROJECT` / `_ENDPOINT`, `COPILOT_REQUEST_DEADLINE_S` (20).
(`COPILOT_BEARER_SECRET` is now **legacy/revoked** — see Authentication below.)

**Week 2 additions (all from `config.py`):**

| Env var | Default | Purpose |
| --- | --- | --- |
| `COHERE_API_KEY` | `""` | RAG embed + rerank (Cohere `embed-v4.0`/`rerank-v3.5`); empty → keyless TF-IDF/Claude fallback |
| `COPILOT_EMBED_PROVIDER` | `auto` | `auto` (Cohere iff key) \| `cohere` \| `local`/`tfidf` |
| `COPILOT_RERANK_PROVIDER` | `auto` | `auto` (Cohere→Claude→Noop ladder) \| `cohere` \| `claude` \| `noop` |
| `COPILOT_RETRIEVAL_TOP_K` | `5` | chunks fed to the model after fusion + rerank |
| `COPILOT_SUPERVISOR_MAX_ROUTES` | `4` | supervisor routing budget per turn |
| `COPILOT_WORKER_MAX_ITERS` | `3` | bounded worker sub-loop cap |
| `COPILOT_SUPERVISOR_MODEL` | *(empty = inherit)* | fast model tier for the routing decision (recommended: Haiku-class) |
| `COPILOT_WORKER_MODEL` | *(empty = inherit)* | fast model tier for worker sub-loops (recommended: Haiku-class) |
| `COPILOT_ANSWERER_MODEL` | *(empty = inherit)* | override for the briefing composer; defaults to `ANTHROPIC_MODEL` |
| `COPILOT_JUDGE_MODE` (`COPILOT_JUDGE`) | `stub` | eval judge: `stub` (hermetic, default) \| `llm` (`--live` boolean judge) |
| `COPILOT_INGEST_ENABLED` | `false` | master switch for `/ingest` + the document viewer endpoints |
| `COPILOT_INGEST_MAX_FILE_MB` | `10` | upload size cap |
| `COPILOT_EXTRACT_MAX_RETRIES` | `2` | VLM schema-invalid re-prompt budget |
| `COPILOT_EXTRACT_MODEL` | `None` | vision model for extraction; `None` → reuse `ANTHROPIC_MODEL` |
| `COPILOT_INGEST_DB_USER` | `""` | INSERT-only writer identity (`copilot_ingest`) — separate from `copilot_ro` |
| `COPILOT_INGEST_DB_PASSWORD` | `""` | writer password (matches the seeder's `__INGEST_PASS__`) |
| `COPILOT_INGEST_DB_DOC_FALLBACK` | `false` | insert the `documents` row directly when the OAuth2 REST path is unavailable (oauth_keys gap) |
| `COPILOT_INGEST_ALLOW_UNVERIFIED` | `false` | **keep false** — unverified extracted values are never persisted while false (fail-closed) |
| `COPILOT_OPENEMR_BASE_URL` (`OPENEMR_BASE_URL`) | `""` | OpenEMR REST base for document upload/download |
| `COPILOT_OPENEMR_OAUTH_CLIENT_ID` | `""` | OAuth2 client id for the REST document front door |
| `COPILOT_OPENEMR_OAUTH_CLIENT_SECRET` | `""` | OAuth2 client secret |
| `COPILOT_OPENEMR_API_USERNAME` | `""` | OAuth2 password-grant username |
| `COPILOT_OPENEMR_API_PASSWORD` | `""` | OAuth2 password-grant password |

**Authentication (Week 2 — session tokens):**

| Env var | Default | Purpose |
| --- | --- | --- |
| `COPILOT_AUTH_DEV_MODE` | `false` | **⚠️ LOCAL ONLY — MUST be `false` in production.** When `true`, exposes `GET /dev/token` (mints a session token without an OpenEMR login) and lets the sidecar fall back to `COPILOT_DEV_SIGNING_KEY` if the DB signing-key row is absent. When `false`, `/dev/token` returns **404** and a missing signing key **fails closed**. |
| `COPILOT_TOKEN_TTL_S` | `900` | session-token lifetime (seconds); `exp = iat + TTL` |
| `COPILOT_DEV_SIGNING_KEY` | _(fixed dev value)_ | HMAC key used **only** in dev mode when the `copilot_signing_key` DB row is missing (logged when used); never used in prod |
| `COPILOT_BEARER_SECRET` | `""` | **legacy / revoked** — the Week-1 static browser bearer. Patient access now requires an OpenEMR-minted token; on production this value must be rotated to an inert string. Retained only for headless/ops mints. |

The token HMAC secret itself is **not** an env var — it lives in the
`copilot_signing_key` table in the `openemr` DB, read by both OpenEMR PHP and the
sidecar (see [`copilot/AUTH_TOKEN_SPEC.md`](copilot/AUTH_TOKEN_SPEC.md) and
[`copilot/MIGRATIONS.md`](copilot/MIGRATIONS.md) §2.4).

Secrets live in Railway private env or the git-ignored `copilot/.env` — never in
code or commits (CLAUDE.md).

### 🔐 Authentication (Week 2)

Week 2 replaces the Week-1 reusable browser bearer with a **short-lived,
OpenEMR-minted, patient-bound session token** — the grader-feedback fix. The
full wire contract is [`copilot/AUTH_TOKEN_SPEC.md`](copilot/AUTH_TOKEN_SPEC.md).

- **OpenEMR mints, after a real ACL check.** Opening a chart runs
  `AclMain::aclCheckCore('patients','demo')` (OpenEMR's own gacl). Pass → a
  900s-TTL HMAC token bound to the user, the selected `pid`, and a `scope`
  (`patient`/`day`) is minted and passed to the panel in the **URL fragment**
  (`#t=<token>`, never sent to a server). Fail → **no panel, no token** ("not
  available for your role").
- **The sidecar verifies, fail-closed, two layers.** `require_session_token`
  recomputes the HMAC over the literal payload segment (→ **401** on any
  failure); `enforce_patient` then requires `scope==patient`, `token.pid ==
  request pid` (a patient-A token on patient B → **403**), `acl=="patients:demo"`,
  and the per-patient provider scope. OpenEMR's gacl already made the role
  decision at mint — the sidecar does not re-implement it.
- **Denial self-test:** in the OpenEMR container,
  `php .../bin/copilot_auth_check.php <username>` runs the real
  `aclCheckCore` — a role-holding user prints `ALLOW` (exit 0), a role-lacking
  user prints `DENY` (exit 3). Sidecar-side hermetic coverage is in
  `copilot/tests/test_session_auth.py`.
- **Dev mode** (`COPILOT_AUTH_DEV_MODE=true`, local only) exposes `/dev/token`
  so the standalone UI and `curl`/load tests can mint a token without a running
  OpenEMR. It **must be false in production**, where `/dev/token` 404s.

### 🧪 Eval gate + LLM judge panel

Two distinct instruments — **do not confuse them:**

- **The hard gate (deterministic, PR-blocking).** 57 boolean-rubric golden cases
  behind the pre-push hook. This is the gate graders' regressions must trip:

  ```bash
  copilot/.venv/Scripts/python.exe copilot/evals/golden/runner.py --mode hermetic \
    --out copilot/evals/golden/results.json
  copilot/.venv/Scripts/python.exe copilot/evals/golden/gate.py   # exit 0 = pass, 1 = blocked
  ```

  Details + the rehearsed regression demo: [`copilot/evals/golden/README.md`](copilot/evals/golden/README.md).

- **The LLM judge panel (on-demand, non-gating).** A 4-lens pairwise A-vs-B
  quality spot-check that runs *beside* the gate and **never** blocks a push
  (`git diff` shows `gate.py`/`baseline.json` untouched; the CLI always exits 0).
  It is an eval-quality instrument, **not** the runtime "critic agent" (that role
  is met by the deterministic `verify()` + `safe_refusal` rubric). Run the
  submission artifact (hermetic stub by default; real judging needs
  `COPILOT_JUDGE=live` + an Anthropic key):

  ```bash
  copilot/.venv/Scripts/python.exe copilot/evals/golden/panel_report.py --mode ablation
  # writes panel_report.json + a markdown summary; other modes:
  #   --compare A.json B.json   # two runner.py outputs + gate-disagreement cross-check
  #   --sample N --seed S       # a sampled safety-critical subset
  ```

  See [`copilot/evals/golden/README.md`](copilot/evals/golden/README.md) and
  `W2_ARCHITECTURE.md` §8.6.

---

_The remainder of this README is the upstream OpenEMR project README._

[![Syntax Status](https://github.com/openemr/openemr/actions/workflows/syntax.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/syntax.yml)
[![Styling Status](https://github.com/openemr/openemr/actions/workflows/styling.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/styling.yml)
[![Testing Status](https://github.com/openemr/openemr/actions/workflows/test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/test.yml)
[![JS Unit Testing Status](https://github.com/openemr/openemr/actions/workflows/js-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/js-test.yml)
[![PHPStan](https://github.com/openemr/openemr/actions/workflows/phpstan.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/phpstan.yml)
[![Rector](https://github.com/openemr/openemr/actions/workflows/rector.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/rector.yml)
[![ShellCheck](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml)
[![Docker Compose Linting](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml)
[![Dockerfile Linting](https://github.com/openemr/openemr/actions/workflows/docker-lint-hadolint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-lint-hadolint.yml)
[![Isolated Tests](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml)
[![Inferno Certification Test](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml)
[![Composer Checks](https://github.com/openemr/openemr/actions/workflows/composer.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer.yml)
[![Composer Require Checker](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml)
[![API Docs Freshness Checks](https://github.com/openemr/openemr/actions/workflows/api-docs.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/api-docs.yml)
[![codecov](https://codecov.io/gh/openemr/openemr/graph/badge.svg?token=7Eu3U1Ozdq)](https://codecov.io/gh/openemr/openemr)

[![Backers on Open Collective](https://opencollective.com/openemr/backers/badge.svg)](#backers) [![Sponsors on Open Collective](https://opencollective.com/openemr/sponsors/badge.svg)](#sponsors)

# OpenEMR

[OpenEMR](https://open-emr.org) is a Free and Open Source electronic health records and medical practice management application. It features fully integrated electronic health records, practice management, scheduling, electronic billing, internationalization, free support, a vibrant community, and a whole lot more. It runs on Windows, Linux, Mac OS X, and many other platforms.

### Contributing

OpenEMR is a leader in healthcare open source software and comprises a large and diverse community of software developers, medical providers and educators with a very healthy mix of both volunteers and professionals. [Join us and learn how to start contributing today!](https://open-emr.org/wiki/index.php/FAQ#How_do_I_begin_to_volunteer_for_the_OpenEMR_project.3F)

> Already comfortable with git? Check out [CONTRIBUTING.md](CONTRIBUTING.md) for quick setup instructions and requirements for contributing to OpenEMR by resolving a bug or adding an awesome feature 😊.

### Support

Community and Professional support can be found [here](https://open-emr.org/wiki/index.php/OpenEMR_Support_Guide).

Extensive documentation and forums can be found on the [OpenEMR website](https://open-emr.org) that can help you to become more familiar about the project 📖.

### Reporting Issues and Bugs

Report these on the [Issue Tracker](https://github.com/openemr/openemr/issues). If you are unsure if it is an issue/bug, then always feel free to use the [Forum](https://community.open-emr.org/) and [Chat](https://www.open-emr.org/chat/) to discuss about the issue 🪲.

### Reporting Security Vulnerabilities

Check out [SECURITY.md](.github/SECURITY.md)

### API

Check out [API_README.md](API_README.md)

### Docker

Check out [DOCKER_README.md](DOCKER_README.md)

### FHIR

Check out [FHIR_README.md](FHIR_README.md)

### For Developers

If using OpenEMR directly from the code repository, then the following commands will build OpenEMR (Node.js version 24.* is required) :

```shell
composer install --no-dev
npm install
npm run build
composer dump-autoload -o
```

### Contributors

This project exists thanks to all the people who have contributed. [[Contribute]](CONTRIBUTING.md).
<a href="https://github.com/openemr/openemr/graphs/contributors"><img src="https://opencollective.com/openemr/contributors.svg?width=890" /></a>


### Sponsors

Thanks to our [ONC Certification Major Sponsors](https://www.open-emr.org/wiki/index.php/OpenEMR_Certification_Stage_III_Meaningful_Use#Major_sponsors)!


### License

[GNU GPL](LICENSE)
