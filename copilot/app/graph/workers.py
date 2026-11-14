"""Graph workers — the `answerer` (the Week 1 tool loop, relocated verbatim)
plus the two bounded specialist workers (intake_extractor, evidence_retriever).

MECHANICAL EXTRACTION NOTE: `SUBMIT_TOOL`, `_Turn`, `_register`, `_run_tool`,
`_finalize`, `_mock_briefing`, and the tool loop (now `answerer`) are the Week 1
`copilot/app/agent.py` code MOVED here, not rewritten. Deliberate deltas only:
- `answerer` takes the graph's shared `_Turn` and an optional wall-clock
  `deadline` so the whole graph lives inside one `request_deadline_s` budget
  (mock check + turn creation moved up to the supervisor);
- an optional `seed_digest` surfaces worker-gathered evidence handles to the
  answerer's prompt;
- `obs.step(...)` became `obs.span(..., node=...)` for per-node attribution;
- Anthropic client construction is a patchable helper (`_anthropic_client`)
  and the client can be passed in (the graph shares one client per turn).

Loop contract (Gate 2 hardening) — unchanged:
- The model drives tool calls; `pid` is injected server-side (never a tool arg).
- Malformed / unknown tool → tool_result(is_error) + retry within budget.
- At the iteration cap OR the wall-clock deadline → force the terminal
  `submit_briefing` tool; if even that fails → a deterministic template briefing
  with `degraded=true`. The loop NEVER raises into the request.

WORKERS: bounded sub-loops with RESTRICTED tool subsets — restriction by
construction: the subset is all the model's request contains. Subsets are
defined by tool NAME and resolved against TOOL_FUNCS at runtime, so a tool
another lane hasn't merged yet (e.g. `search_guidelines`) degrades gracefully
(WorkerResult.ok=False, handoff back to the supervisor) instead of crashing.
Worker summaries are PHI-free BY CONSTRUCTION: counts + handle ids only, never
evidence values — the summary is the only worker output the supervisor LLM
transcript and the handoff payload carry.
"""

from __future__ import annotations

import json
import logging
import re
import time

from ..config import get_settings
from ..contracts import ActionChip, AgentResponse, ChipIntent, Claim, WorkerName, WorkerResult
from ..correlation import get_correlation_id, log_event
from ..critic import critique
from ..evidence import EvidenceSet
from ..observability import TurnObserver
from ..prompts import SYSTEM_PROMPT
from ..tools import TOOL_FUNCS, TOOL_SCHEMAS, ToolContext
from ..verifier import RawClaim, verify

log = logging.getLogger("copilot.agent")

SUBMIT_TOOL = {
    "name": "submit_briefing",
    "description": "Return the final briefing. Every claim MUST cite the handle(s) that support it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "handles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "evidence handles supporting this claim, e.g. ['med:0']",
                        },
                    },
                    "required": ["text", "handles"],
                },
            },
            "action_chips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "intent": {
                            "type": "string",
                            "enum": [i.value for i in ChipIntent],
                        },
                        "params": {"type": "object"},
                    },
                    "required": ["label", "intent"],
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["claims"],
    },
}


class _Turn:
    """Mutable per-turn state: the evidence set + the structured records gathered
    (fed to the Verifier's deterministic constraint scan). Week 2: shared by the
    whole graph — workers and answerer register into ONE evidence set, so the
    verifier at the exit judges everything uniformly."""

    def __init__(self) -> None:
        self.evidence = EvidenceSet()
        self.gathered: dict[str, list] = {
            "encounters": [], "meds": [], "problems": [], "allergies": [], "labs": [],
            "guidelines": [],
        }
        self.tool_failed = False
        # Guideline retrieval that fell back mid-turn (dense-embed → BM25-only,
        # or reranker → Noop). Captured from GuidelineSearchResult so the
        # degradation is a first-class REPORTED state (W2 §9.7), not silent.
        self.retrieval_degraded = False
        self.retrieval_provider: str | None = None
        self.retrieval_reranker: str | None = None


def _register(records, prefix_key: str, turn: _Turn) -> list[dict]:
    out = []
    for rec in records:
        h = turn.evidence.add(rec.HANDLE_PREFIX, rec.source, rec.evidence_text())
        turn.gathered[prefix_key].append(rec)
        out.append({"handle": h, "data": rec.evidence_text()})
    return out


def _run_tool(name: str, args: dict, ctx: ToolContext, turn: _Turn) -> list[dict]:
    """Execute a tool by name (pid from ctx, not args) and register evidence."""
    fn = TOOL_FUNCS[name]
    if name == "get_labs":
        res = fn(ctx, name=args.get("name"))
        return _register(res.labs, "labs", turn)
    if name == "get_care_transitions":
        return _register(fn(ctx).encounters, "encounters", turn)
    if name == "get_medications":
        return _register(fn(ctx).medications, "meds", turn)
    if name == "get_problems_allergies":
        res = fn(ctx)
        return _register(res.problems, "problems", turn) + _register(res.allergies, "allergies", turn)
    if name == "search_guidelines":
        # Reference knowledge, registered under `gl:` handles; the verifier's
        # separation rule keeps gl-only claims out of the patient briefing.
        res = fn(ctx, query=args.get("query") or "")
        # Capture (never drop) the retrieval degradation state so the turn can
        # surface it (obs + AgentResponse). PHI-free: provider/reranker names +
        # a boolean only. `degraded` latches once true across multiple searches.
        if res.degraded:
            turn.retrieval_degraded = True
            turn.retrieval_provider = res.provider
            turn.retrieval_reranker = res.reranker
        return _register(res.chunks, "guidelines", turn)
    raise KeyError(name)


# ---- Current-patient identity guard (QA fix) --------------------------------
# The answerer's context carries client-supplied history that is NOT scoped to the
# current pid, and the patient's name is never in the prompt — so a name from a prior
# cross-patient turn could echo into generated prose. Two defenses: an identity ANCHOR
# in the system prompt (see `answerer`), and this deterministic guard that drops any
# generated text asserting a DIFFERENT patient's identity, disclosed in notes (never
# silent). Both are no-ops when the name can't be resolved (e.g. hermetic tests, no DB).

_FOREIGN_NAME_PATTERNS = [
    re.compile(r"(?:patient|chart)\b[^.\n]{0,40}?\bis\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"),
    re.compile(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*\((?:she|he|they)\b"),
]


def _current_patient_name(ctx) -> str | None:  # noqa: ANN001
    """The in-scope patient's real name, or None if it can't be resolved (no DB / no
    row) — in which case the identity anchor + guard become no-ops."""
    try:
        from ..tools.demographics import get_demographics

        name = get_demographics(ctx).name
    except Exception:  # noqa: BLE001
        return None
    if not name or name == f"Patient {getattr(ctx, 'pid', '')}":
        return None
    return name


def _asserts_foreign_patient(text: str, current_name: str) -> bool:
    """True if `text` asserts a patient identity whose name shares NO token with the
    current patient — i.e. a wrong-patient identity claim. Scoped to identity-assertion
    phrasing to avoid false positives on drug/condition/organization names."""
    if not text or not current_name:
        return False
    cur = {t for t in re.split(r"\s+", current_name.lower()) if t}
    for pat in _FOREIGN_NAME_PATTERNS:
        for m in pat.finditer(text):
            cand = {t for t in re.split(r"\s+", m.group(1).lower()) if t}
            if cand and cand.isdisjoint(cur):
                return True
    return False


def _identity_anchor(current_name: str | None) -> str:
    if not current_name:
        return ""
    return (
        f"\n\nThe ONE patient in scope for this turn is {current_name}. Any other "
        "person-name appearing in the conversation history belongs to a DIFFERENT patient "
        "and must be ignored — never state or imply that this chart belongs to anyone "
        "other than the patient named above."
    )


def _cached_system(static: str, dynamic: str = "") -> list[dict]:
    """System as content blocks with a prompt-cache breakpoint on the STATIC block, so the
    tools + static system prefix are cached across the answerer's 2-6 iterations (and across
    turns) — cutting input reprocessing / latency (QA fix). The dynamic block (the per-patient
    identity anchor) rides AFTER the breakpoint, uncached, so it never busts the shared cache.
    Fake test clients ignore the system shape, so hermetic evals are unaffected."""
    blocks: list[dict] = [{"type": "text", "text": static, "cache_control": {"type": "ephemeral"}}]
    if dynamic:
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def _finalize(
    brief: dict,
    turn: _Turn,
    obs: TurnObserver,
    notes_extra: str | None = None,
    cut_off: bool = False,
    hl=None,
    current_name: str | None = None,
) -> AgentResponse:
    # The model is an untrusted planner: `brief` (its tool input) or the entries
    # of `claims` can be malformed (a bare string instead of a {text, handles}
    # object). Coerce defensively — a malformed claim is dropped and the turn
    # degrades gracefully, never a 500.
    if not isinstance(brief, dict):
        brief = {}
    raw = [
        RawClaim(text=c.get("text", ""), handles=c.get("handles", []) or [])
        for c in brief.get("claims", [])
        if isinstance(c, dict)
    ]
    # Chip DICTS are collected before the critic (it screens labels as free
    # text); typed ActionChip construction stays after the screen.
    chip_dicts = [c for c in (brief.get("action_chips") or []) if isinstance(c, dict)]

    # ---- Critic node (deterministic, always on) — narrows the verifier's
    # input, never rescues; the verifier below remains the exit authority.
    if hl is not None:
        hl.record(
            "answerer", "critic", "critique",
            reason="deterministic pre-verifier review of claims and action chips",
        )
    with obs.span("critic", node="critic"):
        cres = critique(raw, chip_dicts)
    n_uncited = cres.report.rejected_uncited
    n_unsafe = cres.report.stripped_unsafe
    obs.critic_rejected = n_uncited
    obs.critic_stripped = n_unsafe
    if hl is not None:
        hl.record(
            "critic", "answerer",
            "critic_rejected" if (n_uncited or n_unsafe) else "critic_passed",
            reason=f"rejected_uncited={n_uncited} stripped_unsafe={n_unsafe}",
            payload_summary=(
                f"{len(cres.surviving_claims)} claim(s) and "
                f"{len(cres.surviving_chips)} chip(s) forwarded to the verifier"
            ),
        )

    vres = verify(
        cres.surviving_claims, turn.evidence,
        turn.gathered["meds"], turn.gathered["allergies"], turn.gathered["labs"],
    )
    # Quarantine merge: critic-rejected uncited claims land in the SAME
    # quarantine bucket the verifier would have put them in — wire-compatible
    # counts, fail-closed transparency ("unverified", never fact).
    quarantined_all = list(vres.quarantined_claims) + [
        Claim(text=rc.text, source_refs=[]) for rc in cres.rejected_uncited
    ]

    chips: list[ActionChip] = []
    for c in cres.surviving_chips:
        try:
            chips.append(ActionChip(label=c["label"], intent=ChipIntent(c["intent"]), params=c.get("params", {}) or {}))
        except Exception:  # noqa: BLE001 — drop malformed chips, never fail the turn
            continue

    # A turn is degraded if a tool failed or NOTHING survived verification —
    # guideline-only turns (claims partitioned into guideline_evidence) are fine.
    degraded = turn.tool_failed or (
        bool(raw) and not vres.verified_claims and not vres.guideline_evidence
    )
    notes = brief.get("notes") or notes_extra
    # Graceful-degradation honesty (invariant 9): a FORCED / cut-off finalize
    # (deadline blown or the iteration cap hit) that produced NO verified
    # briefing and NO guideline evidence is a partial result, never a silent
    # empty success. `cut_off` gates this so a model that deliberately returns an
    # empty safe refusal WITH time to spare is not mislabeled as degraded.
    if cut_off and not vres.verified_claims and not vres.guideline_evidence:
        degraded = True
        if not notes:
            notes = "Ran out of time this turn — partial result."
    if turn.tool_failed:
        notes = (notes + " " if notes else "") + "Some data was unavailable this turn."
    # Guideline retrieval that degraded mid-turn (Cohere down → BM25-only /
    # NoopReranker) is a supported-but-reported mode (W2 §9.7): the answer is
    # still grounded, but the physician/grader must be told the evidence leg was
    # thinner than usual — never a silent BM25-only answer with degraded=False.
    if turn.retrieval_degraded:
        degraded = True
        prov = turn.retrieval_provider or "?"
        rr = turn.retrieval_reranker or "?"
        notes = (notes + " " if notes else "") + (
            f"Guideline retrieval was degraded (provider={prov}, reranker={rr})."
        )
    # Read-only honesty: stripped recommendations are disclosed, never silent.
    # Clean turns take none of this — notes stay byte-identical to Week 1.
    if n_unsafe:
        notes = (notes + " " if notes else "") + (
            f"Removed {n_unsafe} suggested order/medication action(s) — this "
            "assistant is read-only and never recommends placing or changing orders."
        )

    # ---- Current-patient identity guard: drop any prose that asserts a DIFFERENT
    # patient's identity (context bleed from un-scoped history), disclosed in notes.
    # No-op when `current_name` is unresolved (hermetic/no-DB) — lists stay identical.
    verified = list(vres.verified_claims)
    guideline = list(vres.guideline_evidence)
    if current_name:
        n_before = len(verified) + len(guideline) + len(quarantined_all) + len(chips)
        verified = [c for c in verified if not _asserts_foreign_patient(c.text, current_name)]
        guideline = [c for c in guideline if not _asserts_foreign_patient(c.text, current_name)]
        quarantined_all = [c for c in quarantined_all if not _asserts_foreign_patient(c.text, current_name)]
        chips = [c for c in chips if not _asserts_foreign_patient(c.label, current_name)]
        n_removed = n_before - (len(verified) + len(guideline) + len(quarantined_all) + len(chips))
        if notes and _asserts_foreign_patient(notes, current_name):
            kept = [seg for seg in re.split(r"(?<=[.!?])\s+", notes) if not _asserts_foreign_patient(seg, current_name)]
            notes = " ".join(kept).strip() or None
            n_removed += 1
        if n_removed:
            notes = (notes + " " if notes else "") + (
                f"Removed {n_removed} statement(s) that named a different patient (identity guard)."
            )

    obs.verified = len(verified)
    obs.quarantined = len(quarantined_all)
    obs.constraint_flags = len(vres.constraint_flags)
    obs.degraded = degraded
    obs.retrieval_degraded = turn.retrieval_degraded
    obs.retrieval_provider = turn.retrieval_provider
    obs.rerank_provider = turn.retrieval_reranker

    return AgentResponse(
        correlation_id=get_correlation_id(),
        briefing=verified,
        guideline_evidence=guideline,
        constraint_flags=vres.constraint_flags,
        action_chips=chips,
        quarantined_claims=quarantined_all,
        quarantined_count=len(quarantined_all),
        critic=cres.report,
        degraded=degraded,
        notes=notes,
    )


def _mock_briefing(ctx: ToolContext, turn: _Turn, obs: TurnObserver, hl=None) -> AgentResponse:
    """Deterministic briefing (no LLM) — for load tests. Gathers all tools and
    cites handles honestly so the Verifier still runs."""
    claims = []
    with obs.span("tools", node="answerer"):
        for tname in ("get_care_transitions", "get_medications", "get_problems_allergies", "get_labs"):
            for item in _run_tool(tname, {}, ctx, turn):
                claims.append({"text": item["data"], "handles": [item["handle"]]})
    obs.tool_calls = 4
    # If a specialist worker already registered guideline evidence this turn
    # (the mock evidence route runs the real hermetic search_guidelines), cite
    # each `gl:` chunk verbatim so the verifier partitions it into
    # `guideline_evidence` — this makes the RAG answer path demonstrable in
    # hermetic mock/demo mode. A plain question registers no `gl:` handles, so
    # this loop is empty and the Week 1 mock briefing stays byte-compatible.
    for handle, ev in turn.evidence.items.items():
        if handle.startswith("gl:"):
            claims.append({"text": ev.text, "handles": [handle]})
    return _finalize(
        {"claims": claims, "notes": "Mock LLM briefing (load-test mode)."}, turn, obs, hl=hl
    )


def _anthropic_client(s):
    """Single patchable seam for Anthropic client construction (graph-wide).

    The client carries a per-request timeout derived from the turn budget: the
    SDK default is 600s, which would let a single slow call overrun the ~20s
    `request_deadline_s` by minutes (invariant 9 — graceful degradation, not a
    multi-minute hang). Bounding it here caps EVERY LLM call the graph makes
    (supervisor, workers, answerer all share this one client).

    max_retries=1 (SLOS §2: one transient retry): the SDK default of 2 lets a
    single throttled call stack ~3 attempts x timeout + backoff inside one
    graph span (~67s answerer p50 observed live, 2026-07-16) — the deadline
    check between iterations can't interrupt an in-flight call, so the
    per-call worst case must stay bounded."""
    import anthropic

    timeout = max(1.0, float(getattr(s, "request_deadline_s", 20.0)))
    return anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=timeout, max_retries=1)


def answerer(
    ctx: ToolContext,
    user_text: str | None,
    history: list[dict],
    turn: _Turn,
    obs: TurnObserver,
    deadline: float | None = None,
    seed_digest: list[dict] | None = None,
    client=None,
    hl=None,
) -> AgentResponse:
    """The Week 1 single-agent tool loop, now the graph's terminal node."""
    s = get_settings()
    if client is None:
        client = _anthropic_client(s)
    # Anchor the current patient identity so a name echoed from un-scoped history can't
    # be mistaken for this chart's patient. None (no DB) → no anchor, byte-identical.
    current_name = _current_patient_name(ctx)
    system_blocks = _cached_system(SYSTEM_PROMPT, _identity_anchor(current_name))
    tools = TOOL_SCHEMAS + [SUBMIT_TOOL]
    messages: list[dict] = []
    for h in history[-s.max_history_turns:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("text", "")})
    user_content = user_text or "Brief me on this patient for today's visit."
    if seed_digest:
        # Digest of handles already registered by specialist workers this turn —
        # the answerer cites them under the exact same handle discipline.
        user_content += (
            "\n\nEvidence already gathered by specialist workers this turn "
            "(cite these handles exactly as given; do not re-fetch them): "
            + json.dumps(seed_digest)
        )
    messages.append({"role": "user", "content": user_content})

    if deadline is None:
        deadline = time.perf_counter() + s.request_deadline_s

    for i in range(s.agent_max_tool_iters):
        over_deadline = time.perf_counter() > deadline
        force = (i == s.agent_max_tool_iters - 1) or over_deadline
        # Wall-clock guard (invariant 9): once the deadline is blown AND we have
        # already made at least one call this turn, do NOT start another model
        # call — force-finalize on what we have rather than risk a hang. The
        # first iteration still runs so a fast model can submit under a tight
        # deadline (Week 1 forced-finalize semantics preserved).
        if over_deadline and i > 0:
            return _finalize(
                {"claims": []}, turn, obs,
                notes_extra="Ran out of time this turn — partial result.",
                cut_off=True, hl=hl, current_name=current_name,
            )
        tool_choice = {"type": "tool", "name": "submit_briefing"} if force else {"type": "auto"}
        try:
            with obs.span(f"llm_{i}", node="answerer"):
                resp = client.messages.create(
                    model=s.answerer_model or s.anthropic_model,
                    max_tokens=s.anthropic_max_tokens,
                    system=system_blocks,
                    tools=tools,
                    tool_choice=tool_choice,
                    messages=messages,
                )
            obs.record_tokens(getattr(resp, "usage", None), node="answerer")
        except Exception as exc:  # noqa: BLE001
            obs.error = type(exc).__name__
            log_event(log, "llm_error", error=type(exc).__name__)
            return _finalize(
                {"claims": []}, turn, obs,
                notes_extra="The assistant was unavailable this turn.", hl=hl,
                current_name=current_name,
            )

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            # No tool call — nudge once toward the terminal tool, else finalize.
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": "Call submit_briefing now with your cited claims."})
            continue

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            if tu.name == "submit_briefing":
                # A finalize under a forced tool_choice (deadline/cap) is cut off;
                # under `auto` it is the model's own decision.
                return _finalize(tu.input, turn, obs, cut_off=force, hl=hl, current_name=current_name)
            obs.tool_calls += 1
            # `tu.name` is model-controlled (untrusted planner, invariant 1). An
            # injected/hallucinated name can carry PHI (invariant 8) — never let
            # it into a span name or a log field. Validate against the registered
            # tools BEFORE any span/log; unregistered → a constant label. The real
            # (unvalidated) name still drives _run_tool so the model gets the exact
            # KeyError tool_result and can recover — that goes to the model, not a log.
            safe_name = tu.name if tu.name in TOOL_FUNCS else "unregistered_tool"
            try:
                with obs.span(f"tool_{safe_name}", node="answerer"):
                    data = _run_tool(tu.name, tu.input or {}, ctx, turn)
                content = json.dumps(data)
                is_err = False
            except Exception as exc:  # noqa: BLE001
                turn.tool_failed = True
                obs.tool_failures += 1
                content = json.dumps({"error": type(exc).__name__})
                is_err = True
                log_event(log, "tool_error", tool=safe_name, error=type(exc).__name__)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_err})
        messages.append({"role": "user", "content": results})

    # Cap exhausted without a briefing → deterministic fallback (cut off).
    return _finalize(
        {"claims": []}, turn, obs,
        notes_extra="Could not assemble a full briefing in time.", cut_off=True, hl=hl,
        current_name=current_name,
    )


# ---- Bounded specialist workers (Week 2, D5) ---------------------------------

# Tool subsets by NAME, resolved against TOOL_FUNCS at runtime. A name that is
# not (yet) registered — e.g. `search_guidelines` before the RAG lane merges,
# or a future `get_document_status` — is simply unavailable: the worker
# degrades (ok=False) and hands back; it NEVER crashes the turn.
INTAKE_EXTRACTOR_TOOL_NAMES: list[str] = ["get_document_status", "get_labs"]
EVIDENCE_RETRIEVER_TOOL_NAMES: list[str] = ["search_guidelines"]

_WORKER_PROMPTS: dict[str, str] = {
    "intake_extractor": (
        "You are the intake-extractor worker of a clinical co-pilot graph. The "
        "question references the patient's uploaded documents (intake forms, lab "
        "report PDFs). Ingestion has already landed document-derived facts in the "
        "chart; use ONLY the provided read-only tools to surface that evidence "
        "(each record returns a handle). Gather evidence — do NOT compose the "
        "final answer. Stop calling tools when you have what is relevant."
    ),
    "evidence_retriever": (
        "You are the evidence-retriever worker of a clinical co-pilot graph. Use "
        "ONLY the provided tool to retrieve clinical-guideline evidence relevant "
        "to the question (each chunk returns a handle). Reference knowledge only "
        "— never patient facts. Gather evidence — do NOT compose the final "
        "answer. Stop calling tools when you have what is relevant."
    ),
}


def _worker_loop(
    worker: WorkerName,
    tool_names: list[str],
    max_iters: int,
    ctx: ToolContext,
    turn: _Turn,
    obs: TurnObserver,
    client,
    user_text: str | None,
) -> WorkerResult:
    """Shared bounded sub-loop. `client=None` (mock mode) runs the deterministic
    path: each available tool invoked once with default args. With a client, a
    small LLM loop runs whose request contains ONLY this subset's schemas —
    restriction by construction, not by instruction."""
    available = [n for n in tool_names if n in TOOL_FUNCS]
    before = set(turn.evidence.items)
    iterations = 0
    tokens_in = 0
    tokens_out = 0
    failed = False

    if not available:
        log_event(log, "worker_degraded", tool=",".join(tool_names), error="tools_unavailable")
        return WorkerResult(
            worker=worker,
            ok=False,
            summary=f"degraded: 0 of {len(tool_names)} required tool(s) available; no evidence gathered",
            handles=[],
            iterations=0,
        )

    if client is None:
        # Deterministic path (mock mode) — hermetic, no tokens. The user text
        # rides along as `query` so retrieval-shaped tools (search_guidelines)
        # do real hermetic work; patient tools ignore unknown args.
        for n in available:
            iterations += 1
            try:
                with obs.span(f"tool_{n}", node=worker):
                    _run_tool(n, {"query": user_text or ""}, ctx, turn)
                obs.tool_calls += 1
            except Exception as exc:  # noqa: BLE001
                failed = True
                turn.tool_failed = True
                obs.tool_failures += 1
                log_event(log, "tool_error", tool=n, error=type(exc).__name__)
    else:
        s = get_settings()
        schemas = [t for t in TOOL_SCHEMAS if t["name"] in available]
        known = {t["name"] for t in schemas}
        for n in available:
            if n not in known:
                # Registered callable without a published schema (mid-merge seam):
                # expose a minimal query-shaped schema so the worker can still use it.
                schemas.append({
                    "name": n,
                    "description": f"{n} (restricted worker tool)",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": [],
                    },
                })
        messages: list[dict] = [{
            "role": "user",
            "content": user_text or "Gather the evidence relevant to this patient.",
        }]
        for i in range(max_iters):
            iterations = i + 1
            try:
                with obs.span(f"llm_{worker}_{i}", node=worker):
                    resp = client.messages.create(
                        model=s.worker_model or s.anthropic_model,
                        max_tokens=s.anthropic_max_tokens,
                        system=_WORKER_PROMPTS[worker],
                        tools=schemas,
                        messages=messages,
                    )
                usage = getattr(resp, "usage", None)
                obs.record_tokens(usage, node=worker)
                tokens_in += int(getattr(usage, "input_tokens", 0) or 0)
                tokens_out += int(getattr(usage, "output_tokens", 0) or 0)
            except Exception as exc:  # noqa: BLE001
                failed = True
                log_event(log, "llm_error", error=type(exc).__name__)
                break

            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                break
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                obs.tool_calls += 1
                if tu.name not in available:
                    # Defense in depth: the subset IS the request, but never trust it.
                    results.append({
                        "type": "tool_result", "tool_use_id": tu.id,
                        "content": json.dumps({"error": "tool_not_in_worker_subset"}),
                        "is_error": True,
                    })
                    continue
                try:
                    with obs.span(f"tool_{tu.name}", node=worker):
                        data = _run_tool(tu.name, tu.input or {}, ctx, turn)
                    results.append({
                        "type": "tool_result", "tool_use_id": tu.id,
                        "content": json.dumps(data), "is_error": False,
                    })
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    turn.tool_failed = True
                    obs.tool_failures += 1
                    results.append({
                        "type": "tool_result", "tool_use_id": tu.id,
                        "content": json.dumps({"error": type(exc).__name__}),
                        "is_error": True,
                    })
                    log_event(log, "tool_error", tool=getattr(tu, "name", "?"), error=type(exc).__name__)
            messages.append({"role": "user", "content": results})

    new_handles = [h for h in turn.evidence.items if h not in before]
    ok = (not failed) or bool(new_handles)
    # PHI-free summary BY CONSTRUCTION: counts + handle ids only — never
    # evidence values. This string reaches the supervisor transcript and the
    # handoff payload_summary; nothing else about the evidence does.
    summary = f"{'ok' if ok else 'degraded'}: {len(new_handles)} evidence handle(s) registered"
    if new_handles:
        summary += ": " + ", ".join(new_handles)
    return WorkerResult(
        worker=worker,
        ok=ok,
        summary=summary,
        handles=new_handles,
        iterations=iterations,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def intake_extractor_worker(
    ctx: ToolContext, turn: _Turn, obs: TurnObserver, client=None, user_text: str | None = None
) -> WorkerResult:
    """Surfaces document-derived evidence through existing read-only tools
    (ingestion — Lane A — lands document facts in the chart before the visit;
    this worker consumes them, it never extracts). Max `worker_max_iters`."""
    s = get_settings()
    return _worker_loop(
        "intake_extractor", INTAKE_EXTRACTOR_TOOL_NAMES, s.worker_max_iters,
        ctx, turn, obs, client, user_text,
    )


def evidence_retriever_worker(
    ctx: ToolContext, turn: _Turn, obs: TurnObserver, client=None, user_text: str | None = None
) -> WorkerResult:
    """Retrieves clinical-guideline chunks via `search_guidelines` only (D5:
    max 2 iterations). Degrades gracefully until the RAG lane's tool exists."""
    s = get_settings()
    return _worker_loop(
        "evidence_retriever", EVIDENCE_RETRIEVER_TOOL_NAMES, min(2, s.worker_max_iters),
        ctx, turn, obs, client, user_text,
    )
