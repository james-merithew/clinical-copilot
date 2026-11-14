# Week 2 Architecture Defense — Anticipated Q&A

Prepared answers to the challenges we expect. Companion to [`W2_ARCHITECTURE.md`](../../W2_ARCHITECTURE.md) (section references below). Status: pre-implementation draft, 2026-07-13.

---

## Theme 1 — The write path vs. the read-only invariant

### Q1. "Doesn't writing to `procedure_result` violate your read-only invariant?"

No — the invariant was always "**the agent** never writes," and it still doesn't. All writes live in a deterministic Ingestion Service invoked by a FastAPI endpoint on an explicit human action (a file upload); no model tool call can reach it, and there is no write tool in any tool registry. The agent's DB user (`copilot_ro`) is untouched; the writer is a separate `copilot_ingest` user with INSERT-only grants on exactly five tables plus an in-code table allowlist — two independent enforcements of the same boundary. The LLM's relationship to the database is unchanged from Week 1: it proposes, it holds no handle, it cannot write. What changed is that the *system* now has a second deterministic component, with a narrower privilege than any human user of OpenEMR has. (W2_ARCHITECTURE §3 row 3, §4.2.)

### Q2. "Why direct DB writes instead of FHIR?"

Because the option doesn't exist in this fork: there is no FHIR Observation or DocumentReference **create** path and no REST lab-result create. We verified the API surface — the only REST write we can use is document upload (`apis/routes/_rest_routes_standard.inc.php:496`), and we do use it, so the source document round-trips through OpenEMR's own code. For derived lab facts, the native `procedure_order → procedure_order_code → procedure_report → procedure_result` chain is the *only* way to make extracted values first-class chart rows that OpenEMR's own UI and our Week 1 `get_labs` tool can see. Writing FHIR resources to a sidecar store instead would have created exactly the untraceable, duplicate shadow records the assignment forbids. (§4.2, §12.)

### Q3. "Your direct writes bypass OpenEMR's audit log. How is that defensible?"

Named openly in the tradeoff ledger, with compensations that exceed what the audit log would have captured. Every written row carries: `procedure_result.document_id` linking to the source document, a `copilot.provenance` row (field, page, bbox, quote, confidence), an `ingestion_run` record (content hash, pid, correlation ID), the correlation ID embedded in `procedure_report.report_notes`, structured sidecar logs, and a LangSmith trace. That is field-level lineage; OpenEMR's `log` table records only "user X touched patient Y." The writes are also INSERT-only — the user physically cannot alter or delete existing records — and a production hardening path (writing through OpenEMR's service classes to get audit rows) is noted for later. (§12.)

### Q4. "What stops the intake form from silently corrupting demographics?"

The data-authority rule: one owner per data type, no silent overwrites. `patient_data` is owned by OpenEMR and is **never written** by the ingestion service — demographics from the form are compared and *flagged* on mismatch, for a human to resolve. A patient-reported form is evidence of a discrepancy, not authority over the chart. Meds/allergies go to `lists` (the table OpenEMR itself uses), and facts with no natural OpenEMR home (chief concern, family history) go to the sidecar-owned `copilot.intake_fact` rather than being shoehorned into a fabricated clinical note. (§4.2, §9.1.)

---

## Theme 2 — Vision extraction without invention

### Q5. "How do you know the VLM didn't hallucinate a value?"

Because a hallucinated value cannot pass a check the model doesn't control. Every extracted field must carry a `verbatim_quote` and a claimed page; deterministic code (pypdfium2) then searches the PDF's *real text layer* for that quote on that page, and requires the value to appear inside it. A value the model invented has no matching quote in the document — the field comes back `failed_verification`, is **not persisted**, and is surfaced as a gap. The VLM's output is also schema-constrained (forced tool = the Pydantic JSON schema, `model_validate`, retry ≤ 2), so it can't even *say* anything outside the contract. The model proposes; pdfium disposes. (§4.3.)

### Q6. "What happens with a scanned form that has no text layer?"

Verification is impossible, so persistence is refused — all fields return `needs_review`, nothing is written to any table (`COPILOT_INGEST_ALLOW_UNVERIFIED=false`), and the UI shows a needs-review card with the VLM's boxes rendered *dashed and labeled approximate*, for human orientation only. We deliberately did not add OCR: OCR would create an invention surface exactly where the assignment warns about invention, without a verification story. `lab_scanned.pdf` is a committed fixture, and golden extraction cases assert this exact behavior — so "scan is imperfect" is a rehearsed path, not an edge case. (§4.3 item 6, §13.)

### Q7. "VLM bounding boxes are notoriously unreliable. Why does your UI show boxes at all?"

Two kinds of boxes, visually distinct, with different authority. Verified fields get their bbox **from pdfium's text search** — the same engine that renders the page PNG, so coordinates and pixels can't disagree — drawn solid. VLM-claimed regions are drawn dashed with an explicit "approximate" label and exist only for display on unverified fields. The confidence origin is visible, not asserted: a solid box means "deterministically located in the document," a dashed box means "the model thinks it's here." That distinction *is* the psychology-lens answer to clinician distrust of OCR'd data. (§2.2, §5.3, §12.)

---

## Theme 3 — Grounding and the guideline corpus

### Q8. "How do you prevent guideline text laundering into patient facts?"

Structurally, at the deterministic exit gate — not by prompting. Guideline evidence enters the turn only as `gl:` handles in the EvidenceSet. After normal verification, the verifier partitions claims by handle kind: a verified claim citing **only** `gl:` handles goes to `AgentResponse.guideline_evidence`, rendered under a separate header; **a claim with zero patient-record handles can never appear in the patient briefing**. Mixed claims survive in the briefing only because they cite patient evidence — and their numbers are still ground-checked against the *patient* rows, so "ADA target < 7%" can't morph into "her A1c is 7%." A fabricated `gl:` handle quarantines exactly like a fabricated PK. There is a dedicated golden citation case for each of these paths. (§6.3, §8.1.)

### Q9. "What if Cohere is down during the demo?"

Nothing user-visible breaks; rank quality degrades and the system says so. The provider ladder is: Cohere embed+rerank → hashed TF-IDF + Claude listwise rerank → NoopReranker (RRF order stands); dense-embed failure independently degrades retrieval to BM25-only. The reranker has a 3-second hard timeout. `/ready` reports `reranker: degraded` — a 200 with detail, not an outage — and `/dashboard` shows the fallback engaging. Crucially, grounding never depended on the reranker: every guideline citation still points at a real chunk with real attribution; only ordering quality moves. The whole ladder is exercised in hermetic tests, and mock mode uses TF-IDF+none, so we demo the degradation on request. (§6.2, §9.2, §9.7.)

### Q10. "Where did your guideline corpus come from, and can you legally commit it?"

Hand-distilled, paraphrased "office protocol" markdown — no copyrighted text is committed. Each of the 8–10 files carries YAML front-matter (`doc_id`, `source_org`, `source_url`) naming the source organization (ADA, ACC/AHA, KDIGO, USPSTF, CDC/ACIP), and that attribution renders on every guideline citation in the UI, so the physician can always reach the primary source. The corpus is scoped to the USERS.md persona's actual panel — DM/HTN/CKD/CHF chronic care plus screening and immunizations — because the assignment asks for the practices *this office* follows, not a medical library. Paraphrase drift is a named tradeoff, mitigated by the visible source links. (§6.1, §12.)

---

## Theme 4 — Multi-agent architecture

### Q11. "Why not LangGraph, when the assignment names it?"

The assignment names it as one option among "another inspectable orchestration framework" — inspectability is the graded property, not the dependency. Our graph is three small files with exactly three enum routes; the supervisor's only output channel is a forced routing-tool call, and every transition emits a typed `Handoff` record to three sinks (enum-only logs, LangSmith child runs, the response payload itself). That is arguably *more* inspectable than a framework default, and the assignment's final note warns against framework-collecting. Concretely: Week 1's loop is a hand-rolled raw-SDK loop with a hard deadline, forced finalization, and a hermetic mock mode — porting that machinery into LangGraph mid-sprint buys checkpointing and cycles we don't have, at the cost of re-verifying behavior we already trust. The typed `Handoff`/`WorkerResult` contracts are the seam where LangGraph would slot in if a future week needs a dozen-node cyclic graph. (§7.4.)

### Q12. "How is a multi-agent trace reconstructed from one correlation ID?"

One correlation ID is assigned at the endpoint and threaded — via contextvar — through the supervisor call, every worker invocation, every tool call, the VLM call, the pdfium verification, the DB writes (it's persisted in `procedure_report.report_notes`), and every `Handoff` record. In structured logs, filtering on the correlation ID yields the ordered sequence: authz → routing decisions (`handoff` events with `seq`) → worker tool calls → verification → response summary. In LangSmith, the same ID keys one trace where each worker is a **child span of the supervisor span** and extraction/retrieval sub-calls nest inside their worker. The logs alone are sufficient (Week 1 rule); LangSmith is the dashboard on top. (§3 row 6, §7.3.)

### Q13. "What keeps the supervisor from becoming a black box or looping forever?"

Bounded everything, typed everywhere. The supervisor cannot produce free text — `tool_choice="any"` over exactly three routing tools means its only possible output is a routing decision with a machine-readable route. Budgets are hard: max 4 routes per turn, intake-extractor max 3 iterations, evidence-retriever max 2, all inside the Week 1 20-second wall-clock deadline; exhaustion forces `finalize`, and the answerer runs on whatever evidence was gathered (degraded if thin — never an error page). Workers get restricted tool subsets by construction: the evidence-retriever's request contains only `search_guidelines`, so there is no instruction to disobey. The handoff trail renders in the UI, so even the physician can see how the answer was assembled. (§7.2, §7.3.)

---

## Theme 5 — The eval gate

### Q14. "How does your CI gate catch the regression WE introduce?"

Any change that shifts gated behavior trips a boolean somewhere in 50 cases across five categories, and the gate blocks the push: `gate.py` fails on **>5% regression in any category vs the committed baseline OR any absolute-floor breach** (schema/PHI/refusal 1.00, citation 0.95, factual 0.90). The floors close the slow-ratchet loophole; `--update-baseline` refuses to write a baseline below them, and the baseline is a committed file, so gaming it is visible in the diff. The gate is hermetic (FakeAnthropic transcripts + fixtures, real verifier/graph/schema/PHI machinery), so it runs identically on any machine with zero keys — no "CI was flaky" escape hatch. We ship the demo recipe in `copilot/evals/golden/README.md`: delete the provenance check in `verifier._judge`, `git push`, watch the hook print the per-category diff table and block; restore, push passes. We rehearse the exact scenario the graders will run. (§8.4, §8.5.)

### Q15. "Your gate runs in a pre-push hook. What if there are no GitLab runners — or someone just skips the hook?"

Both named risks. Runner availability on `labs.gauntletai.com` is unknown, so the **pre-push hook is documented as the authoritative gate** and `.gitlab-ci.yml` runs the same jobs as defense in depth when runners exist. On bypass: a hook is a development-workflow gate, not a security control — the graders' test is "does the gate catch a regression before the demo," and it does, demonstrably, on every push from any cloned repo after the one-line install script (`scripts/install-hooks.ps1|.sh`, documented in the README). In a real org this moves server-side (protected branches + required pipelines) — which is exactly the `.gitlab-ci.yml` we already commit. (§8.5, §12 risk 5.)

### Q16. "Why hermetic transcripts instead of evaluating against the live model?"

Because a gate that is slow, flaky, or key-dependent gets bypassed the first week it inconveniences someone — and then it isn't a gate. Hermetic mode replays committed scripted transcripts through FakeAnthropic while the **real** verifier, graph, schema validation, and PHI-log capture run — the deterministic machinery is what the gate protects, and it is fully exercised. `factually_consistent` is a deterministic value-set check, not a judge vibe. Live-model runs exist (`--live`, boolean forced-tool judge) for spot-checking realism, and transcripts are regenerated at integration so they track current behavior. The tradeoff (transcript drift) is named in the ledger and is the right price for a gate that runs in seconds, everywhere, forever. (§8.3, §12.)

---

## Theme 6 — Privacy, scope, and operations

### Q17. "What PHI reaches your logs and LangSmith?"

Logs: none, by construction rather than convention — `log_event` serializes only allowlisted keys (`correlation.py::_ALLOWED_LOG_KEYS`); names, values, quotes, and free text are dropped before they reach the sink. Filenames are explicitly excluded (they contain patient names). Handoff logs carry enum/ID fields only. LangSmith Week 2 spans carry metadata only: IDs, counts, timings, tokens, cost. This is verified, not asserted: the `no_phi_in_logs` rubric runs on every golden case, and log-capture tests assert a full ingest+chat cycle emits no names/values/filenames/quotes. All documents are synthetic reportlab fixtures — demo data only, per the standing invariant; the documented production control for trace content remains BAA + redaction. (§9.9, §3 row 8.)

### Q18. "How do you avoid duplicate records when the front desk uploads the same PDF twice?"

Deterministically, at two levels. `copilot.ingestion_run` has `UNIQUE(content_hash, pid)` on the sha3-512 of the file bytes: a re-upload of the same content for the same patient short-circuits before any store/extract/persist step — zero new rows, and the UI says "already ingested" with links to the existing facts. The hash is the same algorithm OpenEMR itself computes on stored documents (`Document.class.php:1114`), which is also how we resolve `documents.id` after the REST upload — one hash, three jobs (dedup, id-resolution, integrity). Field-level, `copilot.provenance` adds `UNIQUE(document_id, field_id)`, so even a code bug can't double-persist a field. Idempotent re-upload is a golden extraction case. (§4.1, §4.2.)

### Q19. "What's your ingestion latency budget, and why is 30 seconds acceptable?"

p95 < 30s end-to-end, dominated by one VLM vision call — and it is acceptable because **ingestion is off the physician's critical path by design**. The front desk uploads before the visit; extraction runs async; verified facts are ordinary chart rows by the time the physician is between rooms. Her budget is untouched: `/chat` keeps its 20s hard deadline, ~1.5s time-to-first-content, routing ≤1.5s p95, retrieval ≤300ms in-process — and §9.8's baseline comparison against Week 1 numbers proves the shared paths didn't regress. The economics lens, applied: spend seconds where nobody is waiting; spend nothing where the 90-second window lives. (§2.1.)

### Q20. "Who owns each data type?"

One owner per type, in a committed table (§9.1): lab observations → `openemr.procedure_result` (written once, INSERT-only, by the ingestion service); source documents → `openemr.documents` (OpenEMR-managed via its own REST); intake meds/allergies → `openemr.lists`; demographics → `openemr.patient_data`, which we **never write** (compare-and-flag); provenance, ingestion runs, and homeless intake facts → the sidecar-owned `copilot.*` schema; guideline chunks → the repo corpus (git is the database); citation records → ephemeral response payloads in the stateless tier. Each row of the table also states lineage, write access, read access, and validation — no data type has two masters, and no write path overwrites another owner's truth. (§9.1.)

### Q21. "What did you deliberately not build, and why?"

Critic agent (a deterministic fail-closed verifier already rejects uncited claims; a critic model is a weaker gate layered on a stronger one — it lands Wed–Sun as an *additional* reviewer), third document type (the assignment's own pitfall: five types before two work; Wed–Sun on the same pipeline), lab trend chart (polish, not grounding; Wed–Sun), ColQwen2/multi-vector (stretch by the assignment's own words; unjustifiable for a 10-document corpus), LangGraph (Q11), and OCR for scans (every OCR'd value would be unverifiable; fail-closed `needs_review` is the honest behavior). The assignment's final note says the best submissions feel narrower than the spec and stronger because of it — narrowness here is load-bearing: every mechanism we did build is one we can defend line by line. (§13.)
