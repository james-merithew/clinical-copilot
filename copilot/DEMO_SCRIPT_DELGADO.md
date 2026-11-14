# DEMO_SCRIPT_DELGADO.md — single-patient recording cut (3–5 min)

One patient — **Marcus Delgado, pid 101** — carries the entire demo: grounded
briefing, document ingestion (lab PDF **and** medication list), guideline
evidence, click-to-source, the trend chart, the critic, auth, the eval gate,
and the observability walkthrough. The panel never switches patients; the
terminal and the dashboard do the rest. (The full multi-patient reference
script remains `DEMO_SCRIPT.md`.)

- **Surface:** chat panel at **http://127.0.0.1:8811/?pid=101** · dashboard via
  the **chart icon in the panel top bar** · one terminal · LangSmith tab
  (project `clinical-copilot`) logged in.
- **Fixtures:** `copilot/tests/ingest/fixtures/demo_lab_delgado.pdf` and
  `demo_medlist_delgado.pdf`.
- **Talking points in *italics*; the exact thing to type/click in **bold**.**

> **Pre-flight (do before recording):**
> 1. `GET /ready` → `status: "ready"`, all checks true (`vector_index`,
>    `reranker`, `document_storage` included). `degraded` is survivable —
>    call it out; 503 means fix the core check first.
> 2. **Recording insurance:** add `COPILOT_ANSWERER_MODEL=claude-haiku-4-5-20251001`
>    to `copilot/.env` and restart the sidecar. Provider-side throttling of the
>    larger model surfaces as the honest "assistant was unavailable this turn" /
>    partial-briefing notes — correct behavior, but slow TV. On the Haiku tier a
>    full turn lands in a few seconds, and the deterministic verifier (not the
>    model tier) is what guarantees grounding.
> 3. Run one throwaway `Brief me on this patient` turn and confirm it renders
>    with citations before rolling.

---

## Beat 0 — Framing (~20s)

*"An outpatient internist has about **ninety seconds between rooms**. This is
Marcus Delgado — 65, diabetic, hypertensive, kidney function drifting. Watch
one rule the whole way through: the Co-Pilot is **not allowed to tell me
anything it can't trace to a real row in his chart** — and every layer that
enforces that is deterministic code, not model vibes."*

---

## Beat 1 — The briefing (~60s)

**Type:** `Brief me on my 2:15`

- **Point at the citation pills.** *"Every line carries a source —
  `form_encounter`, `prescriptions`, `procedure_result` — bound to actual
  primary keys. This is the chart, attributed, not the model's opinion."*
- **The ED visit.** *"Top of the brief: an **ED visit** since I last saw him —
  the care transition I'd otherwise miss."*
- **The Metformin divergence.** *"His Metformin is recorded in **two tables
  that disagree**. Real EMRs do this constantly. It doesn't silently pick one —
  it **flags the divergence** and cites both rows."*
- **The A1c trend.** *"And the A1c isn't just a value, it's a **trajectory** —
  6.9 to 8.6. The diabetes is getting away from us."*

## Beat 2 — Missing data stays missing (~20s)

**Type:** `What did his cardiologist say at the last visit?`

*"There's no cardiology note in this chart. Watch what it does **not** do: no
invention. The **verifier quarantines** anything it can't bind to a real row,
and the answer says plainly — not in the record. Missing data is surfaced,
never inferred."*

## Beat 3 — The lab PDF becomes chart rows (~50s)

**Upload `demo_lab_delgado.pdf`** (paperclip → Lab report).

- *"The front desk uploaded his lab PDF before the visit. Each value that
  **verified against the document's own text layer** is now a real
  `procedure_result` row with a **citation chip to the exact page region**. A
  value the vision model misreads — `42` where the page says `142` — has no
  matching token in the real text and is **never persisted**. Fail-closed, by
  deterministic code."*
- **Click one citation chip.** *"Click-to-source: the page image with the
  region highlighted. A **solid box** means text-layer-verified — the
  coordinates come from the same engine that read the text."*

## Beat 4 — "What changed?" + the routing trail + the critic (~40s)

**Type:** `what changed for this patient?` → **expand "How this answer was assembled."**

*"The uploaded values are now ordinary cited facts in the briefing. And here's
the multi-agent story with an audit trail: a **supervisor** routed this turn to
workers over **typed handoffs** — and the last two hops are new: every answer
passes a deterministic **critic** that rejects uncited claims and strips
anything shaped like an order or medication recommendation before the verifier
gives the final word. You can see `critique → critic passed` right in the
trail. Cost of that safety hop: a tenth of a millisecond."*

## Beat 5 — Guideline evidence, kept separate (~30s)

**Type:** `what's the target A1c, and what supports it?`

*"His own A1c is a **patient fact**, cited to `procedure_result`. The ADA
target lives under a **separate 'Guideline evidence' header**, cited to a
corpus chunk with source and URL. The verifier **partitions** them — a claim
citing only a guideline can never appear as a fact about Marcus."*

## Beat 6 — The trend chart (~15s)

**Click the `lab trend` chip.**

*"Alongside the text: an **inline trend chart** — hand-rolled SVG, no
libraries. Every point is a real lab row, the uploaded PDF's values included,
and the chart cites its sources just like a sentence."*

## Beat 7 — The medication list, the third document type (~30s)

**Upload `demo_medlist_delgado.pdf`** (paperclip → Med list).

*"Third document type: a pharmacy-style **medication list**. Verified rows land
in the medication problem list — deliberately **never** the `prescriptions`
table, because a fax must not impersonate a prescriber's order. The
**Discontinued** Glipizide is stored inactive, Aspirin's missing frequency is a
**stated gap**, and re-uploading the same file is a **dedup no-op**."*

## Beat 8 — Auth, without leaving his chart (~25s)

**In the terminal** (grab the panel token from the URL fragment, or mint via
`/dev/token?pid=101` in dev mode):

```bash
# Delgado's token used on patient 120 (different provider) -> 403, fail-closed
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOK" \
  -X POST http://127.0.0.1:8811/chat -H "Content-Type: application/json" \
  -d '{"pid":120,"message":"hi"}'          # -> 403
# Tamper one character of the token -> 401
```

*"His token is **bound to him**. Same token, different patient: **403** before
any read. Tampered: **401**. The ACL decision itself was OpenEMR's own
`aclCheckCore` at mint — the sidecar never re-invents permissions."*

## Beat 9 — The gate blocks a sabotage (~40s)

**In the terminal:** change `any(` → `all(` in the provenance line of
`copilot/app/verifier.py`, then:

```bash
copilot/.venv/Scripts/python.exe copilot/evals/golden/runner.py --mode hermetic
copilot/.venv/Scripts/python.exe copilot/evals/golden/gate.py     # or: git push
```

*"I just weakened source attribution in a way that looks like a reasonable
edit. The **65-case golden gate** runs hermetically and fails —
`safe_refusal 11/12` and `no_phi 63/65` under their **1.00 floors** — exit 1,
**push blocked**. Case-identity manifest means deleting a failing case can't
re-green it. Restore the line, gate passes."* **Restore:**
`git checkout -- copilot/app/verifier.py`

## Beat 10 — Observability: one ID, the whole turn (~50s)

**Copy the `correlation_id`** from the "what changed" turn.

1. **LangSmith:** find the trace by that ID. *"The whole turn as a tree:
   supervisor's routing decision, each worker a **child span**, every tool call
   with latency, **LLM calls with token counts and cost**, and the verifier's
   pass/quarantine tally at the end."*
2. **Click the chart icon in the panel's top bar** → the dashboard opens,
   token carried safely in the URL fragment. *"Per-endpoint and **per-node
   p50/p95** — there's the critic at 0.1ms — ingestion pass rate, retrieval hit
   rate, routing outcomes, and the last eval run: **65/65**. All PHI-free by
   construction: route templates, counts, milliseconds. One ID reconstructs a
   turn; one page judges the system."*

## Close (~15s)

*"One patient, ninety seconds: documents verified before they became chart
rows, guidelines kept separate from facts, a critic and a verifier neither of
which is a model, access bound to patient and provider, a gate that blocks
plausible-looking regressions, and every step reconstructable from one ID.
(The same constraint engine also fires CRITICAL drug-interaction flags — ask
me about patient 102.) Read-only. OpenEMR stays the system of record."*

---

### Quick reference — exact inputs (all pid 101)

| Beat | Do |
|---|---|
| 1 | `Brief me on my 2:15` |
| 2 | `What did his cardiologist say at the last visit?` |
| 3 | upload `demo_lab_delgado.pdf` → click a citation chip (bbox) |
| 4 | `what changed for this patient?` → expand the handoff trail (critic hops) |
| 5 | `what's the target A1c, and what supports it?` |
| 6 | click **lab trend** chip |
| 7 | upload `demo_medlist_delgado.pdf` as Med list |
| 8 | terminal: Delgado token vs pid 120 → 403; tampered → 401 |
| 9 | terminal: `any→all` sabotage → gate FAIL → restore → PASS |
| 10 | correlation ID → LangSmith trace → **chart icon** → dashboard |

### If something misbehaves on the day

- **"The assistant was unavailable this turn" / partial briefing:** the
  provider throttled the answerer's model; the graceful-degradation path is
  working as designed. For the recording, set
  `COPILOT_ANSWERER_MODEL=claude-haiku-4-5-20251001` and restart — or re-run
  the turn.
- **Upload 503:** enable `COPILOT_INGEST_DB_DOC_FALLBACK=true` (the documented
  oauth_keys workaround); dedup makes re-upload safe.
- **Gate demo green after the sabotage:** you edited the wrong line — the
  change must hit the verifier's provenance/grounding behavior.
