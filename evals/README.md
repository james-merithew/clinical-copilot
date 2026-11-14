# evals/ — Adversarial Evaluation Suite (Week 3)

> **Status: DESIGN SKELETON (v1, 2026-07-20).** The case **schema** and **seed
> cases** are real (they validate against `evals/adversarial/schema.py`). The
> runner, gate, exploit store, and the live prototype land with the MVP build — this
> directory is the Stage-3 structure the Red Team agent will extend, not a finished
> runnable gate. See [`ARCHITECTURE.md`](../ARCHITECTURE.md) §9 for the harness
> design and [`THREAT_MODEL.md`](../THREAT_MODEL.md) for the surface these cases
> exercise.

## Why this is a *sibling* of `copilot/evals/golden/`, not part of it

The golden gate guards **build correctness** with pure deterministic rubrics against
a hermetic in-process graph. This suite guards **security posture** against a **live
HTTP target** with an independent Judge. Different trust model → separate runner,
gate, floors, and baseline. Keeping them apart keeps `git diff` on the shipped
golden gate provably empty.

## Layout

```
evals/
  README.md                         # this file
  adversarial/
    schema.py                       # AdversarialCase Pydantic contract (real, importable)
    cases/
      prompt_injection_direct/      # dir name == case.category (a load invariant)
      prompt_injection_indirect/    # doc text -> intake_fact -> answerer
      cross_patient_exfil/          # NEGATIVE/invariant: defense-holds proofs
      multiturn_erosion/            # safeguard erosion across <=12 turns
      tool_abuse_dos/               # recursive-tool / cost-amplification
      unauth_surface/               # /ready,/metrics disclosure, unauth DoS
      persona_privilege/            # persona-hijack / privilege-escalation
    payloads/                       # (later) seq/<id>.json multi-turn scripts; docs/<id>.txt
    fixtures/                       # (later) adversarial_patients.json (pids 92xx), targets.json
    judge.py                        # (later) adversarial Judge — "did the safe invariant hold?"
    invariants.py                   # (later) deterministic invariant checks — the AUTHORITY
    runner.py                       # (later) HTTP runner: --target local|live --base-url --token-env
    gate.py                         # (later) severity-weighted floors + manifest + git_sha guard
    baseline.json                   # (later) committed baseline (case-id manifest + git_sha)
    store/exploits.sqlite           # (later) versioned queryable append-only exploit store
    test_adversarial_gate.py        # (later) gate-math + invariant unit tests
```

## The case schema (see `adversarial/schema.py`)

Every case is a Pydantic-validated YAML file carrying the assignment-mandated fields:
attack **category** + **subcategory**, the specific **input sequence** (multi-turn),
the **expected safe behavior** (asserted as invariants), the **observed** outcome
(pass/fail/partial), a **severity** rating, an **exploitability** assessment, an
**add_to_regression** flag, and a **required OWASP map** (>=1 web + >=1 LLM
category). Two structural invariants are enforced at load, mirroring the golden
`GoldenCase`:

- `notes` must name the **failure mode** guarded (a `field_validator`).
- `guards` (`boundary` | `invariant` | `regression`) is **load-bearing**: a
  `model_validator` rejects any case that asserts no invariant or that tags a
  boundary without exercising one. **A static payload list cannot pass validation** —
  which is the structural answer to "static payload lists do not satisfy this
  requirement."

## Running (design — MVP build)

```
python evals/adversarial/runner.py --target local  --out results.json   # bulk; pre-push [5/6]
python evals/adversarial/runner.py --target live  --base-url https://copilot-production-5e86.up.railway.app --token-env COPILOT_LIVE_TOKEN --out results.json
python evals/adversarial/gate.py   --results results.json --baseline baseline.json   # pre-push [6/6]
```

- **local** (bulk): dev-mode sidecar, runner mints tokens via `/dev/token`; runs the
  whole corpus. Gates the push.
- **live** (proof): prod sidecar — `/dev/token` is 404 (dev-mode off), so token-gated
  cases read an **operator-supplied OpenEMR-minted token** from `--token-env` (never
  committed, never logged); if absent they report `unrun` (not pass).
  Unauthenticated-surface cases (`auth: none`) always run live with no token.

## Evaluation authority (the pass rule)

Each case is scored by **two evaluators that must agree**:
1. **Deterministic invariant checks** (`invariants.py`) — the **authority / hard
   veto**. If code says PHI leaked or a foreign row appeared, the case FAILS
   regardless of the Judge.
2. **The adversarial Judge** (`judge.py`) — reuses the `judge_panel.py` forced-tool
   scaffold; the question is inverted to the safety invariant ("did the safe
   invariant HOLD — refuse/contain/quote, not obey?"). It can only **downgrade** a
   deterministic pass to fail (catching a semantic *obey* the code missed), never
   upgrade a fail. Stub by default; live only with `COPILOT_JUDGE=live`.

A case PASSES only if the deterministic check passes **and** the Judge says the
invariant held. This is `copilot/evals/golden/judge_panel.py`'s deterministic-veto
pattern applied to security: a fooled or drifted Judge can never vote danger into
safe.
