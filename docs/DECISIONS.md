# DECISIONS — running log (this is the memory)

> Append every non-obvious decision here: what was decided, why, and the
> alternative rejected. Auto-loaded via CLAUDE.md so the next session and every
> sub-agent start warm instead of re-litigating. Newest at the top.

## 2026-07-21 — Week-3 MVP readiness pass (closed 4 partials + honesty)
Audited the shipped platform against all 17 Week-3 MVP requirements (all 4 HARD GATES pass:
deployed target live, THREAT_MODEL, ARCHITECTURE, evals+live agent). Closed the 4 partials:
(1) **typed error contract** — one versioned `error.v1.json` envelope discriminating all
five required failure modes (`target_unreachable`/`budget_exceeded`/`judge_timeout`/
`no_findings`/`regression_detected`) via `error_type` + `retryable`, rather than five
near-identical files; covered by the existing producer/consumer/no-drift suite. (2) **≥3
vuln reports** — materialized from REAL live probes of the deployed target (4 unauthenticated
info-disclosure findings: `/ready`, `/openapi.json`, `/docs`, `/dashboard`) via the real
DocumentationAgent, plus a defense-held control (`/metrics/summary`→401); added the missing
clinical-impact field to the agent. **Rejected** fabricating findings to hit the count —
honesty is the graded standard. (3) **platform cost analysis** at 100/1K/10K/100K TEST RUNS
(distinct from the physician-scaled copilot analysis): the load-bearing point is that the
deterministic invariant/gate/store layers are **$0**, so cost scales **sub-linearly** — the
generator is the only token cost, and regression is deterministic replay. (4) **README**
run-against-live commands. Plus an **honesty pass**: replaced ARCHITECTURE.md's stale "no
code ships" banner with a shipped-in-MVP vs planned-for-Final table (multi-lens/order-swap
judge, cross-category regression, drift canary, cost/ordering observability are Final-scope,
explicitly not MVP-required). **Rejected** implementing those deep features now — the
requirements doc says the MVP is the foundation, not the finished platform.

## 2026-07-21 — Week-2 rubric hardening pass (foundation for Week 3)
Acted on the graded Week-2 rubric (86/100, passed): closed 7 lost-point lines, chosen for
point impact AND for being the muscles Week 3 inherits. Most were "the capability exists
but the PROOF was never shown," so the fixes are honest measurements/tests, not new
features. Shipped on `feat/week3` (since merged to `main`): (1) a full-path E2E integration test (upload→extract→
retrieve→answer) that stitches the two halves the golden runner tested separately, by
pointing the ingest writer AND the read-only tool layer at ONE SQLite engine so a chat
turn cites the just-ingested `procedure_result` PK; (2) a HARD-GATE end-to-end proof — a
REAL product regression (verifier `_judge` forced fail-open, invariant #4 removed) drives
the actual runner→`gate.py` to exit 1, upgrading the synthetic-dict unit tests to the
grader's own scenario; (3) a standalone PHI scanner (`scripts/phi_scan.py`) as a named
CI/pre-push check — case-sensitive sentinel match so the capitalized PHI value-form trips
but the lowercase allowlisted source-key slug (`field_id hemoglobin_a1c.value`) does not,
value-blind findings; (4) committed 65-case results + gate-report SNAPSHOTS so latest
results are reviewable at HEAD; (5) aggregate retrieval quality (recall@1 0.79 / @3 0.97 /
@5 1.00, MRR 0.90) over a 17-query labeled set with floor+drift+coverage gates; (6) a
runner-less security-audit fallback (`security_audit.sh`, single source CI+local calls) —
surfaced 10 REAL CVEs (1 pytest dev-only, 7 starlette runtime-transitive via
`fastapi==0.115.6` pinned `<0.42`), handled by a documented, self-justifying
`--ignore-vuln` allowlist (accepted+tracked; demo/synthetic data, private sidecar) that
still fails on any NEW advisory; (7) a reranker ON-vs-OFF ablation. **Two non-obvious
decisions worth recording:** (a) the reranker ablation MEASURED that on the 9-doc
high-signal corpus the RRF first stage is near-optimal (MRR 0.902 vs a 1.000 oracle,
+0.098 headroom) and reranking a pool that small *reduces* MRR — keyless lexical −0.142,
live Cohere rerank-v3.5 −0.044 — so **shipping keyless (noop) is the measured-correct
default**, reranker kept as a swappable stage behind the harness (measuring it beat
assuming a reranker always helps); **rejected** turning the reranker on by default without
evidence. (b) The starlette CVEs were **allowlisted+tracked, not force-fixed** — a
FastAPI major bump right before submission risks the deployed app; **rejected** bumping
the web framework now and report-only (weaker gate). All committed-eval artifacts are
keyless/CI-reproducible; live numbers (Cohere) live in `RESULTS.md` as dated operator runs.

## 2026-07-21 — Repo packaging: carve tests/tools back into `git archive` exports
Week-2-final feedback: a clean eval run failed before the gate because
`copilot/app/tools`, `copilot/tests`, and `tests/ingest/_helpers.py` were missing from
the exported repo. **Root cause (proven, not guessed):** OpenEMR's inherited
`.gitattributes` marks `tests/` + `tools/` `export-ignore` with **unanchored** patterns,
so they strip those paths (and `redteam/tests`) from **`git archive`** — which backs
GitHub/GitLab "Download ZIP" and submission tarballs — while leaving them committed.
`git archive HEAD -- copilot/app/tools | tar -tf-` gave **0** entries; `git ls-tree`
showed them present. clone/checkout/GitLab-CI/Railway never honor `export-ignore`, which
is exactly why every live flow passed and only the zip broke. **Fix:** append
directory-level `-export-ignore` carve-outs (`copilot/app/tools/`, `copilot/tests/`,
`redteam/tests/` + `**`) — last-match-wins, the same technique OpenEMR uses for its
`EHI_Export` docs. **Regression guard** (guards the *class* of bug — tracked-but-unexported):
`scripts/verify_clean_archive.sh` extracts `git archive HEAD` and runs **both gates from
the export**; a `clean-archive` CI job runs it (the `eval` job runs from a clone and
structurally cannot catch this); a cheap pre-push `[0/6]` archive-presence check fails
fast locally. Verified: guard trips on `HEAD~1` (pre-fix), passes on `HEAD`; clean export
→ 431 tests + golden 65/65 + adversarial gate all PASS. **Rejected:** editing the global
`tests/`/`tools/` lines (would un-ignore OpenEMR's own dev dirs — scope creep + drift risk);
renaming our dirs out of the pattern (churns imports, loses OpenEMR-convention alignment).

## 2026-07-20 — Week 3 MVP build: sequential, live-anchored, honest
Built the adversarial platform (Red Team + Judge + Orchestrator + Documentation +
deterministic invariants + versioned regression store + runner + gate) as a **sequential
in-context build**, not the planned parallel-worktree fan-out. **Why:** the fan-out
orphaned agents twice across process restarts (~8 lost runs, only 1 salvaged); a
sequential build fails loudly and holds up. **Rejected:** re-running worktree agents.
Key sub-decisions (from a three-lens review + a cold critic): (a) the deterministic
invariants are the authority; the Judge's LLM lenses are **escalate-only** (can raise
the attacker verdict, never downgrade a real success) so a fooled/drifting lens can't
manufacture a false-safe verdict; (b) observability lives **self-contained in `redteam/`**
(not bolted onto the co-pilot `MetricsSummary`) so the golden 65-case gate is untouched;
(c) findings carry **provenance** (`seeded` vs `mutation_discovered`) and are never sold
as a novel 0-day — the honest framing that survives the interview; (d) **hermetic gate =
"the harness works"; the live run is the only evidence about the target** — never
conflated; (e) `critic.stripped_unsafe > 0` is the co-pilot's defense *working*
(telemetry), not a violation. The live proof lands a **real** finding (`/ready` discloses
the internal integration map to an unauthenticated caller, RT-005 medium) that the
hermetic stub's bare `/ready` does not — a clean real-vs-stub split. Models: Sonnet 5
generator / Opus 4.8 pinned Judge / **not Fable 5** (its cyber classifier refuses
offensive prompts).

## 2026-07-20 — Week 3 file layout: root docs + rename W1 ARCHITECTURE.md
The Week-3 adversarial platform's docs land at repo root; the hard-gate path
`./ARCHITECTURE.md` becomes the **platform** doc, so the Week-1 doc was renamed
`W1_ARCHITECTURE.md` and its 17 inbound references across 10 files (incl.
`verifier.py`, `contracts.py`, `test_constraint_engine.py`, `openapi.json`) were
repointed; `openapi.json` was regenerated (drift test green). Platform code will live
in a new top-level `redteam/` package; inter-agent schemas in `contracts/`; attack
suite in `evals/`. **Why:** the grader's hard gates name the literal paths
`./THREAT_MODEL.md`, `./ARCHITECTURE.md`, `./evals/`; the refs genuinely point to
Week-1 content so repointing keeps them accurate. **Rejected:** a `W3_ARCHITECTURE.md`
+ thin pointer (leaves the hard-gate path as the Week-1 doc — fails the literal gate).

## 2026-07-20 — Reuse the in-house handoff graph for the adversarial platform
The four platform agents (Orchestrator, Red Team, Judge, Documentation) coordinate
over the Co-Pilot's existing typed-handoff graph (`copilot/app/graph/`, `Handoff` at
`contracts.py:361`), extending `NodeName`/`RouteName` Literals — not LangGraph/CrewAI/
AutoGen. **Why:** inspectability + log-safety were already the chosen requirement
(prior W1–2 entry); a heavyweight scheduler is a black box with a free-text-in-logs
risk and no matching need. **Rejected:** adopting a framework (dependency with no
requirement it serves).

## 2026-07-20 — Anthropic default, Red-Team generator behind a typed Protocol
All four roles run on Anthropic via `_anthropic_client` + per-role model tiers, but the
Red-Team **attack generator** sits behind a typed Protocol (mirroring
`rag/providers.py`) so a local/OSS uncensored model can slot in later without touching
call sites. **Why:** answers the "frontier models refuse offensive work" concern — the
deterministic Judge veto is the quality guarantee regardless of generator, so a
refusing generator yields *fewer* attacks, never *false findings*; the OSS swap is a
contract-stable future move, not a rewrite. **Rejected:** hard-coding one provider;
adopting an uncensored model as the default now (unjustified given BAA/PHI posture).

## 2026-07-20 — Judge = deterministic veto over votes; regression pass = invariant held
The adversarial Judge reuses `judge_panel.py` (independent lenses, order-swap debias,
a safety veto **computed not voted**); a case PASSES only if a deterministic invariant
check AND the Judge agree, and the Judge can only **downgrade** a deterministic pass,
never upgrade a fail. A regression test passes iff the **safe invariant held** across
the attack's mutation family — never because a payload string went absent (the
`evals/` `model_validator` rejects any case asserting only string-absence). **Why:** a
behavior change that dodges the literal payload must still fail; a fooled/drifted Judge
must never vote danger into safe. **Rejected:** an LLM-judge-only pass authority (a
weaker gate on a stronger deterministic one); string-match regression (false passes).

## 2026-07-20 — Human gates on critical-report filing + remediation; deterministic budget
Attack generation, dispatch, judging, regression runs, and non-critical report filing
run autonomously; **filing a CRITICAL report** and **any remediation trigger** require
human sign-off; the Orchestrator's budget ceiling and stop conditions are deterministic
(AI proposes only mutation strategy, never spend). **Why:** a critical finding is the
highest-consequence output and remediation changes the target under test (the
platform stays a read-only observer — the platform-level analogue of invariant #3); an
AI orchestrator must not talk itself past the spend ceiling. **Rejected:** fully
autonomous filing (a confident false positive wastes engineering time); a fully-AI
orchestrator (can rationalize past budget).

## 2026-07-16 — Per-node model tiers + bounded SDK retries
Routing/worker LLM calls run on a Haiku-class tier
(`COPILOT_SUPERVISOR_MODEL`/`COPILOT_WORKER_MODEL`, empty = inherit); the
physician-facing answerer keeps the main model. `_anthropic_client` passes
`max_retries=1`. **Why:** measured turn is LLM-dominated; routing is a forced
3-way tool choice and workers are bounded tool loops — neither needs
answerer-class reasoning, and the deterministic verifier is the quality
guarantee regardless. The SDK default of 2 retries let one throttled call stack
~3× the timeout inside a single span. **Rejected:** dropping the deadline (would
hurt the graceful-partial guarantee).

## 2026-07-16 — /ready reports document_storage, vector_index, reranker
Cheap config/corpus-coherence probes, degraded (200) not 503. **Why:** rubric +
grader feedback. **Caught a real defect:** the `vector_index` probe failed on
first prod deploy — the Dockerfile never copied `corpus/`, so prod retrieval ran
empty. Fixed by `COPY corpus ./corpus`. **Rejected:** calling `get_index()` in
the probe (would trigger the lazy build on the health path).

## 2026-07-16 — medication_list persists to `lists`, never `prescriptions`
Third document type. Extracted meds → `lists` (`type='medication'`), same
surface as intake current-meds. **Why:** a document-reported med must not gain
prescriber authority; `tools/medications.py` treats `prescriptions` as the
ordered-authority row (AUDIT.md divergence rule). No writer allowlist/grant
change needed. **Rejected:** writing to `prescriptions` (silent authority
upgrade).

## 2026-07-16 — Critic is a deterministic node, before the verifier
4th graph node after the answerer, before `verify()`. Rejects uncited claims
(merged into `quarantined_claims` for wire-compat) and strips unsafe
order/med-change suggestions. No LLM, always on. **Why:** the doc names a critic
deliverable; the verifier stays the exit authority (critic only narrows its
input). **Rejected:** an LLM critic (a weaker model gate layered on a stronger
deterministic one); `CriticReport.model_review` left as a seam.

## Prior (Week 1–2 baseline)
Hand-rolled typed-handoff graph over LangGraph (inspectability is the
requirement, not a framework; W2 §7.4). Deterministic verifier as exit gate.
Session-token auth replacing the reusable browser bearer. Token-alignment (not
substring) extraction verification — `42` never matches inside `142`.
