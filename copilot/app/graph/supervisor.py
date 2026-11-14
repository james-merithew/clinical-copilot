"""Routing supervisor — one LLM whose ONLY possible output is a routing decision.

W2_ARCHITECTURE.md §7.2: the supervisor call carries exactly three tools
(`route_to_intake_extractor`, `route_to_evidence_retriever`, `finalize`) with
`tool_choice={"type": "any"}`, so there is no free-text channel through which
routing can become un-inspectable. Budgets: `supervisor_max_routes` decisions
per turn, all inside the Week 1 `request_deadline_s` wall clock — on budget or
deadline exhaustion the graph force-finalizes (`route=forced_finalize`) and the
answerer runs on whatever evidence was gathered. Every transition emits a
Handoff (see graph/handoff.py). Mock mode routes deterministically by keyword
so tests and k6 load runs stay hermetic.
"""

from __future__ import annotations

import logging
import time

from ..config import get_settings
from ..contracts import AgentResponse
from ..correlation import log_event
from ..observability import TurnObserver
from ..tools import ToolContext
from . import workers as _workers
from .handoff import HandoffLog
from .workers import (
    _mock_briefing,
    _Turn,
    answerer,
    evidence_retriever_worker,
    intake_extractor_worker,
)

log = logging.getLogger("copilot.graph")

# Exactly 3 routing tools — the supervisor's entire action space. NOTE: like
# every model-facing schema, these expose no `pid`/`user_id` (injection guard).
ROUTING_TOOLS: list[dict] = [
    {
        "name": "route_to_intake_extractor",
        "description": (
            "Route to the intake-extractor worker when the question references the "
            "patient's uploaded documents (intake form, lab report PDF, scan, "
            "paperwork) or facts derived from them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "One line: why this route (shown in the trace/UI, never logged).",
                }
            },
            "required": ["reason"],
        },
    },
    {
        "name": "route_to_evidence_retriever",
        "description": (
            "Route to the evidence-retriever worker when clinical-guideline "
            "reference evidence would help (targets, thresholds, screening "
            "intervals, recommendations)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "One line: why this route (shown in the trace/UI, never logged).",
                }
            },
            "required": ["reason"],
        },
    },
    {
        "name": "finalize",
        "description": (
            "All needed specialist evidence is gathered (or none is needed) — hand "
            "off to the answerer to compose the cited briefing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "One line: why finalize now (shown in the trace/UI, never logged).",
                }
            },
            "required": ["reason"],
        },
    },
]

SUPERVISOR_PROMPT = """\
You are the routing supervisor of a clinical co-pilot graph assisting a \
primary-care physician. You NEVER answer the question yourself — you only \
decide, one step at a time, which node runs next, by calling exactly one of \
your routing tools.

- route_to_intake_extractor: the question references the patient's uploaded \
documents (intake form, lab report PDF, scan) or document-derived facts.
- route_to_evidence_retriever: clinical-guideline reference evidence would \
help (targets, thresholds, screening intervals, recommendations).
- finalize: specialist evidence is gathered or not needed — the answerer will \
gather patient-chart data itself and compose the cited briefing.

Workers report back a summary of the evidence handles they registered. Route \
to each worker at most once unless its result was clearly insufficient. When \
in doubt, finalize — the answerer has full read access to the chart.
"""

_WORKERS_BY_ROUTE = {
    "route_to_intake_extractor": ("intake_extractor", intake_extractor_worker),
    "route_to_evidence_retriever": ("evidence_retriever", evidence_retriever_worker),
}

# Deterministic mock routing (hermetic tests / k6): keyword → route.
_MOCK_DOC_WORDS = ("document", "upload", "uploaded", "intake", "form", "pdf", "scan", "paperwork")
_MOCK_EVIDENCE_WORDS = ("target", "should", "screening", "recommend", "guideline")


def _run_worker(worker_name, worker_fn, ctx, turn, obs, hl, client, user_text, route, reason):
    """Route to a worker and record both transitions (route out, worker_return)."""
    hl.record("supervisor", worker_name, route, reason=reason)
    w0 = time.perf_counter()
    wr = worker_fn(ctx, turn, obs, client=client, user_text=user_text)
    hl.record(
        worker_name,
        "supervisor",
        "worker_return",
        reason=f"{worker_name} finished ok={wr.ok} after {wr.iterations} iteration(s)",
        payload_summary=wr.summary,
        latency_ms=round((time.perf_counter() - w0) * 1000, 1),
    )
    return wr


def _mock_graph_turn(
    ctx: ToolContext, user_text: str | None, turn: _Turn, obs: TurnObserver, hl: HandoffLog
) -> AgentResponse:
    """Deterministic keyword routing + the Week 1 mock briefing. A plain
    question takes the exact Week 1 path (no handoffs) — byte-compatible."""
    text = (user_text or "").lower()
    routed = False
    if any(w in text for w in _MOCK_DOC_WORDS):
        _run_worker(
            "intake_extractor", intake_extractor_worker, ctx, turn, obs, hl,
            None, user_text, "route_to_intake_extractor",
            "mock keyword routing: document terms in the question",
        )
        routed = True
    elif any(w in text for w in _MOCK_EVIDENCE_WORDS):
        _run_worker(
            "evidence_retriever", evidence_retriever_worker, ctx, turn, obs, hl,
            None, user_text, "route_to_evidence_retriever",
            "mock keyword routing: guideline terms in the question",
        )
        routed = True
    if routed:
        hl.record("supervisor", "answerer", "finalize", reason="mock routing complete")
    resp = _mock_briefing(ctx, turn, obs, hl=hl)
    resp.handoffs = hl.handoffs
    return resp


def run_graph_turn(
    ctx: ToolContext, user_text: str | None, history: list[dict], obs: TurnObserver
) -> AgentResponse:
    """The graph turn: supervisor routing loop → workers → answerer → verifier
    (via the answerer's `_finalize`, the single exit — unchanged semantics)."""
    s = get_settings()
    turn = _Turn()
    hl = HandoffLog(obs)
    deadline = time.perf_counter() + s.request_deadline_s

    if s.mock_llm:
        return _mock_graph_turn(ctx, user_text, turn, obs, hl)

    client = _workers._anthropic_client(s)

    sup_messages: list[dict] = []
    for h in history[-s.max_history_turns:]:
        sup_messages.append({"role": h.get("role", "user"), "content": h.get("text", "")})
    sup_messages.append(
        {"role": "user", "content": user_text or "Brief me on this patient for today's visit."}
    )

    routes_used = 0
    finalized = False
    while routes_used < s.supervisor_max_routes and time.perf_counter() <= deadline:
        try:
            with obs.span(f"supervisor_{routes_used}", node="supervisor"):
                resp = client.messages.create(
                    model=s.supervisor_model or s.anthropic_model,
                    max_tokens=1024,
                    system=SUPERVISOR_PROMPT,
                    tools=ROUTING_TOOLS,
                    tool_choice={"type": "any"},
                    messages=sup_messages,
                )
            obs.record_tokens(getattr(resp, "usage", None), node="supervisor")
        except Exception as exc:  # noqa: BLE001
            obs.error = type(exc).__name__
            log_event(log, "llm_error", error=type(exc).__name__)
            break  # → forced_finalize; the answerer still runs (graceful degradation)

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            # tool_choice="any" makes this near-impossible — defend anyway.
            log_event(log, "routing_error", error="no_tool_use", seq=hl.next_seq)
            break

        tu = tool_uses[0]
        routes_used += 1  # every decision consumes budget → guaranteed termination
        reason = str((tu.input or {}).get("reason", "") or "")

        if tu.name == "finalize":
            hl.record("supervisor", "answerer", "finalize", reason=reason or "supervisor finalized")
            finalized = True
            break

        picked = _WORKERS_BY_ROUTE.get(tu.name)
        if picked is None:
            # Unknown routing tool name (model drift) → routing_error, retry.
            log_event(
                log, "routing_error", error="unknown_route",
                tool=str(getattr(tu, "name", "?"))[:40], seq=hl.next_seq,
            )
            sup_messages.append({"role": "assistant", "content": resp.content})
            sup_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": "unknown route; call one of the provided routing tools",
                    "is_error": True,
                }],
            })
            continue

        worker_name, worker_fn = picked
        wr = _run_worker(
            worker_name, worker_fn, ctx, turn, obs, hl, client, user_text, tu.name, reason
        )
        # The worker's PHI-free summary (counts + handle ids ONLY) is all the
        # supervisor transcript ever learns about the evidence.
        sup_messages.append({"role": "assistant", "content": resp.content})
        sup_messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tu.id, "content": wr.summary}],
        })

    if not finalized:
        hl.record(
            "supervisor", "answerer", "forced_finalize",
            reason="route budget or deadline exhausted — forcing the answerer",
        )

    seed = turn.evidence.digest() if not turn.evidence.is_empty() else None
    resp = answerer(
        ctx, user_text, history, turn, obs,
        deadline=deadline, seed_digest=seed, client=client, hl=hl,
    )
    resp.handoffs = hl.handoffs
    return resp
