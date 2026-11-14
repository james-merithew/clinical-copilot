# USERS.md — Clinical Co-Pilot

**Purpose.** This document defines *who* the Clinical Co-Pilot is for, the concrete workflow it enters, and the specific use cases it addresses. It is the **source of truth** that `W1_ARCHITECTURE.md` traces back to: every agent capability we build must point to a use case here. If a capability has no use case below, we don't build it.

---

## 1. The user (narrow and concrete)

**The physician** — an **outpatient internal-medicine primary-care physician** with a **full patient schedule** (~20 patients/day, 15–20-minute slots) and a **90-second between-rooms workflow**.

Her day is a mix of chronic-disease management (diabetes, hypertension, CKD, CHF), acute visits, annual wellness checks, medication refills, and post-hospital follow-ups. She does **not** deeply pre-read charts — there is no time — so she is often orienting herself as she walks in.

**Her real failure mode is not "can't find data." It is *missing what mattered*:** an ED visit she didn't know about, a specialist's new recommendation buried in a note, an overdue screening, or a new medication another provider started that interacts with one of hers. The data is all present — that's the problem. It is too much, too fast, under a patient's waiting gaze.

We deliberately reject the vague "physicians need help finding information." That thesis has produced a thousand failed health-tech products. This user is specific, and that specificity constrains everything: what data the agent needs, how fast it must respond, what it must refuse to do, and what "useful" even means.

---

## 2. The 90-second moment (the workflow the agent enters)

- **The 30 seconds before she opens it:** she has just closed the prior visit and is walking down the hall. She knows only the next patient's **name** and a **rough chief complaint**.
- **What she needs from it:** *who is this, why are they here, what changed since the last visit, and what can't I afford to miss today?*
- **What she does with the output:** walks in oriented, addresses the right things, doesn't get blindsided, and doesn't drop a care gap.

This is why the surface is a **multi-turn conversation embedded in the OpenEMR chart** — she asks, then follows up (typed or tapped), **not a separate app or dashboard**. She stays in the EHR, and the agent surfaces context beside her existing workflow.

---

## 3. Use cases (each with *why an agent*)

Every use case states explicitly why a conversational agent is the right shape — not a dashboard, a sorted list, or a better chart view.

### UC-1 — Between-room briefing
*"Brief me on my 2:15."* → reason for visit, what changed since last visit, active problems, can't-miss items.
- **Why an agent:** her follow-ups are unpredictable and **branch conversationally under pressure** — "wait, why was he in the ED?" → "what's his A1c trend?" A dashboard can't anticipate that branch.
- **Requires:** multi-turn conversation + tool chaining. *(Justifies those capabilities in `W1_ARCHITECTURE.md`.)*

### UC-2 — Care-gap / can't-miss surfacing
*"Anything overdue or flagged?"* → overdue screenings, abnormal-but-unaddressed labs, interaction/allergy flags.
- **Why an agent:** relevance is **contextual to today's visit** — what matters for a diabetic's visit differs from an acute back-pain visit. The agent reasons about relevance to *today* rather than dumping a static list.
- **Requires:** domain-constraint checks (verification layer) + reasoning over retrieved records.

### UC-3 — Targeted grounded lookup
*"Last three A1c values and dates?"* / *"Is she due for a mammogram?"*
- **Why an agent:** faster than clicking through EHR tabs, and **every answer is source-attributed** — the verification story made visible at the point of care.
- **Requires:** typed tools + hard source attribution (verifier).

---

## 4. What the agent must refuse or not do

The user's needs also define the agent's *boundaries*:

- **Read-only.** It surfaces context; it never writes, mutates, or places orders. (Reduces clinical blast radius.)
- **This patient only.** It is not a generic medical Q&A bot — it knows *this* patient, and only patients the requester is authorized to see.
- **No unattributed claims.** Anything it can't bind to a real record is held, not stated as fact.
- **Sensitive-data caution.** Segmentation of specially protected records (e.g., 42 CFR Part 2) is a known gap, parked as a hardening item.

---

## 5. Tolerances (what "useful" means for her)

| Dimension | Requirement |
| --- | --- |
| Latency | Seconds, not minutes — time-to-first-content ≈ 1.5s; depth deferred to follow-ups |
| Trust | Every claim source-attributed; unattributed claims withheld; a wrong answer can harm |
| Failure | Graceful degradation — partial + flagged, never fabricated |
| Interaction | Multi-turn conversation is the core interface; free text and chips are co-equal entries to the same agent; chips are suggested-reply accelerants that lower blank-box friction, not a replacement for typing |

---

## 6. Traceability (capability → use case)

| Agent capability | Traces to | Justification |
| --- | --- | --- |
| Multi-turn free-text conversation (core interface) | UC-1 | Follow-ups branch unpredictably under pressure; typed free text is a first-class path |
| Action Chips (suggested-reply accelerants) | UC-1 | Pre-composed likely next turns that lower blank-box friction — an accelerant on the conversation, never a replacement for typing |
| Tool chaining | UC-1, UC-2 | One question's answer seeds the next (briefing → "why the ED visit?" → labs) |
| Care-gap / can't-miss reasoning | UC-2 | Relevance is contextual to *today's* visit |
| Grounded targeted lookup | UC-3 | Faster than tab-clicking; every answer attributed |
| Verification / source attribution | UC-1, UC-2, UC-3 | A confident hallucination can harm the patient |
| Read-only tools only | §4 | Reduces clinical blast radius; the agent surfaces, never mutates |

**Out of scope for MVP:** pre-clinic panel scan (fast-follow over the same tools), write-back / order placement, generic medical Q&A, multi-agent orchestration.
