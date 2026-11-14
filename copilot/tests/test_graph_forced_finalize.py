"""Hermetic tests for the graph's graceful exits (W2 D5, §7.2).

Failure modes guarded:
- (boundary) route-budget exhaustion → `forced_finalize` handoff, answerer
  still runs, response is well-formed — the graph can never spin forever;
- (boundary) wall-clock deadline already exhausted → supervisor is skipped
  entirely, `forced_finalize`, and the answerer's first call is the FORCED
  terminal `submit_briefing` (the Week 1 deadline behavior, one level up).
"""

from __future__ import annotations

import pytest

from app.contracts import AgentResponse
from app.correlation import set_correlation_id
from app.tools import ToolContext
from graph_utils import fake_resp, submit_briefing_resp, tool_use


@pytest.fixture
def ctx():
    set_correlation_id("graph-ff-cid")
    return ToolContext("graph-ff-cid", 1, 101)


def _run(ctx, user_text="brief me"):
    from app.agent import run_turn
    from app.observability import TurnObserver

    obs = TurnObserver(101, "message")
    return run_turn(ctx, user_text, [], obs), obs


def test_forced_finalize_on_route_budget_exhaustion(
    real_llm_env, stub_tools, fake_client_factory, ctx, monkeypatch
):
    """A supervisor that never finalizes burns its supervisor_max_routes (4)
    budget and the graph force-finalizes. search_guidelines is removed from
    the registry so each evidence route degrades without consuming worker LLM
    calls — keeping the call arithmetic purely about the route budget."""
    monkeypatch.delitem(stub_tools, "search_guidelines", raising=False)
    fake = fake_client_factory([
        fake_resp(tool_use("route_to_evidence_retriever", {"reason": "r1"}, id="t1")),
        fake_resp(tool_use("route_to_evidence_retriever", {"reason": "r2"}, id="t2")),
        fake_resp(tool_use("route_to_evidence_retriever", {"reason": "r3"}, id="t3")),
        fake_resp(tool_use("route_to_evidence_retriever", {"reason": "r4"}, id="t4")),
        submit_briefing_resp(),  # answerer after forced finalize
    ])

    resp, _ = _run(ctx)

    assert isinstance(resp, AgentResponse)
    routes = [h.route for h in resp.handoffs]
    # 4 × (route out + worker_return) + the forced exit + the critic trail.
    assert routes == (
        ["route_to_evidence_retriever", "worker_return"] * 4
        + ["forced_finalize", "critique", "critic_passed"]
    )
    assert [h.seq for h in resp.handoffs] == list(range(11))
    forced = resp.handoffs[8]
    assert (forced.from_node, forced.to_node) == ("supervisor", "answerer")
    # Exactly 4 supervisor calls + 1 answerer call — never a 5th route.
    assert fake.consumed == 5


def test_forced_finalize_on_deadline(
    real_llm_env, stub_tools, fake_client_factory, ctx, monkeypatch
):
    """Deadline exhausted before any routing → the supervisor loop never runs;
    one forced_finalize handoff; the answerer is immediately FORCED to the
    terminal submit_briefing tool (Week 1 deadline semantics preserved)."""
    from app.config import get_settings

    monkeypatch.setenv("COPILOT_REQUEST_DEADLINE_S", "0")
    get_settings.cache_clear()

    fake = fake_client_factory([submit_briefing_resp()])

    resp, _ = _run(ctx)

    assert [h.route for h in resp.handoffs] == ["forced_finalize", "critique", "critic_passed"]
    assert fake.consumed == 1
    # The single (answerer) call was the forced terminal tool.
    assert fake.calls[0]["tool_choice"] == {"type": "tool", "name": "submit_briefing"}
