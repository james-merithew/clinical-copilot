# DEMO_SCRIPT.md — Clinical Co-Pilot (3–5 min)

A tight, defensible walkthrough of the physician-facing chat UI. Every beat is
chosen to demonstrate one thing a hospital CTO would ask about: **is it grounded,
is access control real, and can I see what it did?**

- **Surface:** the chat panel at **http://127.0.0.1:8808/** (served by the sidecar).
  No secret is baked into the page: the panel reads a **session token from the URL
  fragment** — in production OpenEMR mints it after an ACL check; for this local
  standalone demo run with `COPILOT_AUTH_DEV_MODE=true` so the panel mints one via
  `/dev/token` (see Beat 7 and `AUTH_TOKEN_SPEC.md`).
- **Data:** synthetic. Showcase A = **pid 101** ("the 2:15"), Showcase B = **pid
  102** (the verifier money-shot), **pid 23** = the authorization denial fixture.
- **Second screen/tab:** the **LangSmith** project `clinical-copilot` open, for the
  closing trace.
- **Total budget:** ~4 minutes. Talking points are in *italics*; the exact thing to
  type/click is in **bold**.

> **Pre-flight (do before recording):** `GET /ready` returns 200 with `status:
> "ready"` — core checks (`database`, `llm`, `observability`) green plus the
> dependency checks (`vector_index`, `reranker`; with ingest enabled also
> `openemr_api`, `ingest_db`, `document_storage`). `degraded` is survivable but
> call it out; 503 means fix the failing core check before rolling. Open the
> panel on pid 101 and confirm one live briefing renders; confirm pid 23 denies.

---

## Beat 0 — Framing (~20s)

*"This is an outpatient primary-care physician. Twenty patients a day, and about
**ninety seconds between rooms** to figure out what changed for the next one. That's
not enough time to reread a chart — but it's exactly enough time to be dangerous if
you walk in blind. The Co-Pilot lives inside OpenEMR, read-only, and its whole job is
to make that ninety seconds safe. The rule I care about most: it is **not allowed to
tell you anything it can't trace back to a real row in the chart.**"*

---

## Beat 1 — Showcase A, pid 101: the briefing (~90s)

**Open the panel on pid 101.** Type into the chat box:

> **Brief me on my 2:15**

*While it streams the response, narrate:*

- **Point at the citations.** *"Every single line carries a source. This one is from
  `form_encounter`, this med is from `prescriptions`, this lab from
  `procedure_result` — each bound to the actual primary key. Nothing here is the
  model's opinion; it's the chart, attributed."*
- **The ED visit.** *"Top of the briefing: he had an **ED visit** since I last saw
  him. That's the care transition I'd otherwise miss between rooms."*
- **The Metformin divergence.** *"Here's the one I love. His **Metformin** is
  recorded in two places — the `prescriptions` table and the `lists` table — and they
  **disagree**. Real EMRs diverge like this constantly. The Co-Pilot doesn't silently
  pick one; it **flags the divergence** and cites both rows so I know to reconcile it."*
- **The A1c trend.** *"And it pulled the **A1c trend** — not just the latest value,
  the trajectory — so I can see the diabetes is drifting the wrong way."*

**Then type a follow-up:**

> **why the ED visit?**

*"Multi-turn — it keeps context. It explains the ED encounter and cites the same
`form_encounter` row. Same grounding rule on a conversational follow-up."*

**Then click the `care gaps` chip** (a suggested-reply accelerant under the answer).

*"A chip tap is the same turn as typing — it just saves me the keystrokes. Here it
surfaces the **overdue screening**. That's the can't-miss item for today."*

---

## Beat 2 — Showcase B, pid 102: the Verifier money-shot (~75s)

**Switch the panel to pid 102.** Type:

> **Anything I should watch for?**

*Point at the constraint flags:*

- **CRITICAL allergy interaction.** *"This is the headline. The patient has a
  documented **penicillin allergy** and an **active amoxicillin** prescription.
  Amoxicillin *is* a penicillin — a naive string match would miss that, so we carry a
  **drug-class map**. The Co-Pilot fires a **CRITICAL** flag and cites both the
  allergy row and the medication row."*
- **Drug–drug flag.** *"Second flag: **warfarin + ibuprofen** — a real bleeding-risk
  interaction. Also fired by the deterministic constraint engine, not the language
  model's memory."*

**Now ask something that isn't in the record:**

> **What did his cardiologist say at the last visit?**

*"There's no cardiology note in this chart. Watch what it does **not** do — it
doesn't invent one. The **Verifier quarantines** any claim it can't bind to a real
row, and the Co-Pilot **surfaces the gap**: 'not in the record — needs chart review.'
Missing data is shown as missing. It never fabricates to sound complete."*

---

## Beat 3 — Authorization denial, pid 23 (~30s)

**Switch the panel to pid 23** (or trigger the pid-23 request from the API
collection) and send any prompt.

*"Access control is real, not cosmetic. My identity as the requesting physician is
**pinned on the server** — the client can't spoof who's asking. pid 23 belongs to a
**different provider**, so the Authorization Gateway refuses **before any data is
read** — a clean **403**, fail-closed, and the denial is **audit-logged** with who
asked, which patient, and the correlation ID. The agent never holds more access than
the physician using it."*

---

## Beat 4 — The trace (~45s)

**Copy the `correlation_id`** from the pid-101 briefing you just ran (it's on the
response payload; the panel shows it in the turn's details row). **Switch to the
LangSmith tab** (project `clinical-copilot`) and **find the trace by that ID**.

*"Everything is observable, and one ID ties it together. This is the exact turn you
just watched, found by its **correlation ID**. One trace per turn: the
**authorization check**, each **tool call** and how long it took, the **LLM calls
with token counts and cost**, and the **verification result** — how many claims
passed, how many were quarantined. The same correlation ID is on every structured
log line for this request — and the logs are **PHI-free by construction**: source
keys and counts, never patient values. If a physician ever asks 'why did it say
that,' I can reconstruct the whole turn from one ID. That's the difference between
a demo and something you'd put in front of a real clinician."*

---

## Close (~15s)

*"Ninety seconds, twenty patients: a briefing where every claim is traceable to the
chart, contraindications caught deterministically, missing data surfaced instead of
invented, access control enforced server-side, and every step observable. Read-only,
OpenEMR stays the system of record. That's the Clinical Co-Pilot."*

---

### Quick reference — exact inputs

| Beat | Patient | Type / click |
|---|---|---|
| 1 | pid 101 | type `Brief me on my 2:15` → type `why the ED visit?` → click **care gaps** chip |
| 2 | pid 102 | type `Anything I should watch for?` → type `What did his cardiologist say at the last visit?` |
| 3 | pid 23 | type any prompt → expect **403 denied** |
| 4 | — | LangSmith → open the pid-101 trace by correlation ID |

### If something misbehaves on the day

- **Briefing looks thin / a tool errored:** the response sets `degraded=true` and
  says data was unavailable — that's the graceful-degradation path, call it out
  rather than hiding it. Re-run once.
- **pid 23 returns 200 instead of 403:** the demo user's `providerID` scoping is off
  — stop and fix before recording; the denial is a load-bearing claim.
- **LangSmith trace missing:** tracing is best-effort/non-fatal, so `/chat` still
  works; if the trace didn't land, show a prior trace rather than a blank panel.

---

# Week 2 walkthrough — multimodal evidence (3–5 min)

The Week 1 script above is the grounded text briefing. This Week 2 addendum shows
the **document → chart → answer** loop, the **guideline evidence** separation, the
**click-to-source** overlay, the **ops dashboard**, and the **eval gate blocking a
sabotage**. Same rule throughout: *nothing reaches the physician that isn't traced
to a real row, a real document region, or a cited guideline chunk.*

- **Surface:** the same chat panel at **http://127.0.0.1:8808/**, plus the ops
  dashboard at **/dashboard** and a terminal for the gate demo.
- **Pre-flight:** `COPILOT_INGEST_ENABLED=true`, `COHERE_API_KEY` set (or accept the
  keyless fallback), the `copilot` schema seeded (`live_seed.sql`), and `GET /ready`
  returns 200 with the full Week-2 dependency set green — `openemr_api`, `ingest_db`,
  `document_storage`, `vector_index`, `reranker` (or `degraded` — call it out; a
  keyless run honestly reports `reranker: false` + `degraded`).
  Fixture: `copilot/tests/ingest/fixtures/demo_lab_delgado.pdf` (a synthetic lab PDF
  for **pid 101**).

## W2 Beat 0 — Framing (~15s)

*"The front desk uploads documents before the visit. The important recent
information for my 2:15 isn't a chart row yet — it's in a lab PDF. Week 2 ingests
that PDF, extracts the values, and — this is the part a CTO cares about — it
**cannot persist a value it can't verify against the document's own text**. By the
time I'm between rooms, the document's facts are ordinary chart rows my briefing
already surfaces, at zero wait."*

## W2 Beat 1 — Upload + extraction with gaps and bbox (~60s)

**Upload `demo_lab_delgado.pdf` for pid 101** (the paperclip in the composer, or the
API collection's `POST /ingest`).

*While the upload-result card renders, narrate:*

- **Persisted facts carry citations.** *"Each extracted lab value that verified is
  now a real `procedure_result` row, and every one carries a **citation chip** back to
  the exact page region it came from."*
- **Gaps are stated, not hidden.** *"Notice it says a field was **not found** on the
  document — it tells me the gap in words instead of silently dropping it. And if a
  value's quote didn't match the document's text layer, it's **failed verification —
  not persisted**. A value the vision model misread — say `42` where the page says
  `142` — has no matching token in the real text, so it never reaches the database.
  That's deterministic code the model can't argue with."*
- **The dedup notice.** *"Upload the same file again and it's a **no-op** — `deduplicated`,
  zero new rows. Idempotent by `(content_hash, pid)`."* (Optionally re-upload to show it.)

## W2 Beat 2 — "What changed?" → supervisor + worker handoffs (~60s)

**Type into the chat box (pid 101):**

> **what changed for this patient?**

*Point at the response:*

- **The document's facts are now in the briefing.** *"The lab values I just uploaded
  show up as ordinary, cited chart facts — same briefing, richer because the PDF is
  now part of the record."*
- **The handoff trail.** *"Expand 'how this answer was assembled.' A **supervisor**
  routed this turn: it sent it to the **intake-extractor** worker for the document
  facts, and to the **evidence-retriever** worker for the guideline. Every hop is a
  **typed handoff** — inspectable, not a black box. That's the multi-agent story with
  an audit trail."*

## W2 Beat 3 — Guideline evidence, kept separate (~45s)

**Ask an evidence question (pid 101):**

> **what's the target A1c, and what supports it?**

*Point at the two regions of the answer:*

- **Separate header.** *"This is the line I care about most for trust. The patient's
  own A1c is in the **briefing**, cited to `procedure_result`. The **ADA target < 7%**
  is under a **separate 'Guideline evidence' header**, cited to a corpus chunk with the
  source org and URL. I never have to wonder whether 'target < 7%' is a fact about
  *this patient* or a line from a protocol — the **verifier partitions** them. A claim
  citing only a guideline can **never** appear as a patient fact."*

## W2 Beat 4 — Click-to-source bbox overlay (~30s)

**Click a document citation chip** on one of the persisted lab values.

*"This opens the **page image** with the exact region **highlighted**. A **solid
box** means the value was deterministically verified against the PDF's text layer —
the coordinates come from the same engine that read the text. Trust made visible: I
can see the source without leaving the panel. (A dashed box would mean 'approximate,
model-claimed only' — shown for orientation, never persisted as fact.)"*

## W2 Beat 5 — The ops dashboard (~30s)

**Open /dashboard.**

*"A grader — or an on-call engineer — can judge system health without reading a
log line. Per-endpoint and **per-graph-node** p50/p95, ingestion count and
**field-level verification pass rate**, retrieval hit rate, routing outcomes, and
the **last eval run's pass/fail per category**. It's fed by `/metrics/summary`,
which is **PHI-free by construction** — route templates and counts only."*

## W2 Beat 6 — The eval gate blocks a sabotage (~45s)

**In the terminal, introduce a regression** — e.g. weaken the verifier's provenance
check (the `any(...)` → `all(...)` one-liner that lets a claim citing one real *and*
one fabricated handle verify), then run the gate:

```bash
copilot/.venv/Scripts/python.exe copilot/evals/golden/runner.py --mode hermetic
copilot/.venv/Scripts/python.exe copilot/evals/golden/gate.py       # or: git push (the pre-push hook runs both)
```

*"This is the safety net. I just broke source attribution in a way that looks like a
reasonable fix. The **57-case golden gate** runs hermetically — no keys, no DB — and
`citation_present` / `factually_consistent` cross their **tightened floors** (0.97 /
0.96). `gate.py` exits **non-zero with a per-category diff table**, and the **pre-push
hook blocks the push**. Restore the line and it goes green. Any grader-introduced
regression that changes gated behavior trips the same wire — including the subtle
one-liner our own adversarial review found."*

## W2 Beat 7 — Authentication & role-based denial (~45s)

*This is the grader-feedback fix: no reusable browser bearer; a short-lived,
OpenEMR-minted, patient-bound token, and the real OpenEMR ACL decides access.*

**Positive path (in OpenEMR, not the standalone panel):** log in to OpenEMR as
**Dr. Chen** (a clinician with `patients|demo`), open **Marcus Delgado's** chart.

*"When I opened this chart, OpenEMR ran its own ACL check — `aclCheckCore` — and
only then minted a **short-lived token** bound to me and to this patient, and
handed it to the panel in the URL fragment, which never hits a server log. The
panel's chat and upload work because that token verifies. There's no reusable
secret sitting in the browser anymore."*

**Denial path — pick one:**

- **In the UI:** log in as an **Accounting / Front-Office** user (a role that
  lacks `patients|demo`) and open the Co-Pilot menu / My Day. *"Same system, no
  clinical role — OpenEMR mints **no token**, and the page says plainly
  **'not available for your role.'** The denial is authoritative at the source,
  not something the sidecar guesses at."*
- **Or in a terminal** (crisper for a recording), run the ACL self-test in the
  OpenEMR container:

  ```bash
  su -s /bin/sh apache -c "php .../oe-module-copilot/bin/copilot_auth_check.php <accounting_user>"
  # -> aclCheckCore('patients','demo','<user>') = DENY   (exit 3)
  # a clinician user prints ALLOW (exit 0)
  ```

  *"The real OpenEMR ACL says DENY, exit code 3 — the exact 'a valid user who
  lacks the role is refused' check. And on the sidecar, a token for patient A
  used on patient B is a clean **403**, expired/tampered tokens are **401**."*

## W2 Beat 8 — LLM judge panel spot-check (~30s, optional)

*Frame it honestly as an eval-quality instrument, not the runtime critic.*

**In a terminal:**

```bash
copilot/.venv/Scripts/python.exe copilot/evals/golden/panel_report.py --mode ablation
```

*"One more layer on the eval story — a **4-lens LLM judge panel** (rigor,
brevity, format, security) that compares our real answer against a deliberately
**weakened** variant per case and checks the panel prefers the better one. It's a
**spot-check report, never the gate** — it always exits 0, and `git diff` shows
the gate untouched. The hard gate stays the deterministic 57-case set from Beat 6.
This is about proving *quality*, and it stays off the pass/fail wire on purpose."*

## W2 Beat 8b — Early-submission extensions (~40s, optional but recommended)

Two quick flashes of the extension deliverables:

- **Lab trend chart.** On pid 101, **click the `lab trend` chip**. *"Alongside the
  text answer, the panel now renders an **inline trend chart** — no charting
  library, no external calls. Every point is a real `procedure_result` row, and
  the latest point carries the standard **citation pill** — the chart obeys the
  same grounding rule as every sentence. The values extracted from the uploaded
  lab PDF are in the series too, because verified extractions became ordinary
  chart rows."*
- **Third document type.** **Upload `demo_medlist_delgado.pdf` as a Medication
  list** (or run API-collection request 12). *"Week 2's third document type: a
  pharmacy-style **medication list**. Verified rows land in the problem-list
  medication surface — deliberately **never** the `prescriptions` table, because
  a document-reported med must not impersonate a prescriber order. The
  **Discontinued** row is stored inactive, the missing frequency is a stated
  gap, and the fail-closed verification is the same engine the lab PDF used."*

## W2 Beat 9 — Observability walkthrough: one ID, the whole turn (~75s)

*This is the grader-requested walkthrough: reconstruct a full multi-agent turn
from the correlation ID alone, live on screen.*

**Run one more turn on pid 101** (e.g. re-ask `what changed for this patient?`)
and **copy its `correlation_id`**.

1. **LangSmith** (project `clinical-copilot`): **search by the correlation ID**
   and open the trace. Walk the tree top-down, pointing as you go:
   - *"Top of the trace: the turn itself — pid, kind, and the same correlation
     ID."*
   - *"The **supervisor** span: its routing decision is recorded as a **typed
     handoff** — here it routed to the **intake-extractor** and
     **evidence-retriever** workers; each worker invocation is a **child span
     of the supervisor**, so the graph structure is literally visible in the
     trace."*
   - *"Inside each worker: every **tool call** as its own span with latency —
     you can see exactly which chart rows were read and how long each read
     took."*
   - *"The **LLM calls** carry **token counts and cost** — this turn cost a
     fraction of a cent, and the cost report is built from these numbers, not
     guesses."*
   - *"And the last stop before the user: the **verifier** — claims checked,
     claims quarantined. Nothing reaches the screen without passing here."*
2. **The logs tell the same story, PHI-free.** Show the structured log lines
   for that correlation ID (terminal or log viewer): *"Every line is JSON with
   the correlation ID, event name, source keys, and counts — **never a patient
   value**. The observability pipeline is allowed to know *what happened*, not
   *what the patient's A1c is*."*
3. **`GET /ready`** (browser or curl): *"Readiness is per-dependency, not a
   binary: database, LLM, observability, **vector index**, **reranker**,
   **document storage**, the OpenEMR API and ingest DB — an ancillary outage
   reports **degraded**, and chat keeps working."*
4. **Close on /dashboard:** *"And the aggregate view: per-endpoint and
   **per-graph-node p50/p95**, ingestion and field-verification pass rate,
   retrieval hit rate, routing outcomes, and the last eval run. One turn is
   reconstructable from one ID; the whole system is judgeable from one page."*

## W2 Close (~15s)

*"Same ninety seconds, now with the documents the front desk uploaded folded into
the briefing — verified before they became chart rows, cited to the page region,
guideline evidence kept separate from patient facts, every routing hop inspectable,
access minted by OpenEMR's own ACL and scoped to one patient, and a gate that
blocks a plausible-looking regression before it can ship. Read-only, OpenEMR stays
the system of record."*

---

### Week 2 quick reference — exact inputs

| Beat | Where | Do |
|---|---|---|
| W2-1 | pid 101 | upload `demo_lab_delgado.pdf` (paperclip / `POST /ingest`) → read the result card (citations, gaps, dedup) |
| W2-2 | pid 101 | type `what changed for this patient?` → expand the handoff trail |
| W2-3 | pid 101 | type `what's the target A1c, and what supports it?` → point at the separate Guideline evidence header |
| W2-4 | pid 101 | click a document citation chip → bbox overlay (solid = text-layer verified) |
| W2-5 | — | open `/dashboard` |
| W2-6 | terminal | apply the `any→all` verifier mutation → run runner + `gate.py` (or `git push`) → gate blocks; restore → green |
| W2-7 | OpenEMR | log in as Dr. Chen → open Delgado (token minted, panel works); log in as an Accounting-role user → "not available for your role", **or** `copilot_auth_check.php <user>` → DENY / exit 3 |
| W2-8 | terminal | `panel_report.py --mode ablation` → panel prefers the stronger answer; always exits 0 (non-gating) |
| W2-8b | pid 101 | click **lab trend** chip → inline SVG chart w/ citation pill; upload `demo_medlist_delgado.pdf` as **Med list** → rows in `lists` (never `prescriptions`), Discontinued → inactive, missing frequency → gap |
| W2-9 | LangSmith + browser | copy a turn's `correlation_id` → find the trace → walk supervisor → workers → tool spans → LLM tokens/cost → verifier; show PHI-free logs for the same ID → `GET /ready` per-dependency body → `/dashboard` |

### If something misbehaves on the day (Week 2)

- **`/ingest` 403:** pid not owned by the pinned demo provider — use pid 101.
- **`/ingest` 503 / store failure:** OAuth (`oauth_keys`) gap on the fork — enable
  `COPILOT_INGEST_DB_DOC_FALLBACK=true` for the demo, or follow `deploy/RUNBOOK.md`
  to register the client; re-upload (dedup makes it safe).
- **No guideline evidence:** `COHERE_API_KEY` unset falls back to keyless TF-IDF —
  retrieval still works; if a turn shows `degraded=true`, call it out as the honest
  fallback, not a bug.
- **Gate demo shows green after the mutation:** you edited the wrong line — the
  sabotage must change *gated* behavior (verifier provenance/grounding); re-check the
  diff table's per-category rates.
