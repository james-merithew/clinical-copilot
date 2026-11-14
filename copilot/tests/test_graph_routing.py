"""Hermetic tests for the supervisor routing protocol (W2 D5, §7.2).

Failure modes guarded:
- (invariant) each of the 3 routing tool calls parses into the correct node
  transition with typed Handoff records;
- (boundary) an unknown routing tool name → `routing_error` log + recovery,
  never a crash;
- (invariant) a worker whose tool subset is absent from TOOL_FUNCS degrades
  (ok=False, handoff back) instead of crashing — the Lane-B merge seam;
- (regression/security) routing tools expose no pid/user_id argument.
"""

from __future__ import annotations

import json

import pytest

from app.contracts import AgentResponse
from app.correlation import set_correlation_id
from app.tools import ToolContext
from graph_utils import fake_resp, submit_briefing_resp, text_block, tool_use


@pytest.fixture
def ctx():
    set_correlation_id("graph-test-cid")
    return ToolContext("graph-test-cid", 1, 101)


def _run(ctx, user_text="hello"):
    from app.agent import run_turn
    from app.observability import TurnObserver

    obs = TurnObserver(101, "message")
    return run_turn(ctx, user_text, [], obs), obs


def test_route_to_intake_extractor_parsed(real_llm_env, stub_tools, fake_client_factory, ctx):
    """route_to_intake_extractor → worker sub-loop (restricted tools) → return."""
    fake = fake_client_factory([
        # supervisor decision 1
        fake_resp(tool_use("route_to_intake_extractor",
                           {"reason": "user references an uploaded intake form"}, id="tu_s1")),
        # intake worker sub-loop: one tool call, then stop
        fake_resp(tool_use("get_labs", {}, id="tu_w1")),
        fake_resp(text_block("enough gathered")),
        # supervisor decision 2
        fake_resp(tool_use("finalize", {"reason": "evidence in hand"}, id="tu_s2")),
        # answerer
        submit_briefing_resp(),
    ])

    resp, _ = _run(ctx, "summarize the uploaded intake form")

    assert isinstance(resp, AgentResponse)
    routes = [h.route for h in resp.handoffs]
    assert routes == [
        "route_to_intake_extractor", "worker_return", "finalize",
        "critique", "critic_passed",
    ]
    assert [h.seq for h in resp.handoffs] == [0, 1, 2, 3, 4]
    assert (resp.handoffs[0].from_node, resp.handoffs[0].to_node) == ("supervisor", "intake_extractor")
    assert (resp.handoffs[1].from_node, resp.handoffs[1].to_node) == ("intake_extractor", "supervisor")
    assert (resp.handoffs[2].from_node, resp.handoffs[2].to_node) == ("supervisor", "answerer")
    assert (resp.handoffs[3].from_node, resp.handoffs[3].to_node) == ("answerer", "critic")
    assert (resp.handoffs[4].from_node, resp.handoffs[4].to_node) == ("critic", "answerer")
    # Worker gathered evidence under the shared handle discipline.
    assert "lab:0" in resp.handoffs[1].payload_summary
    # Restriction by construction: the worker request carried ONLY its subset
    # (get_document_status is not registered, so only get_labs is present).
    worker_call = fake.calls[1]
    assert [t["name"] for t in worker_call["tools"]] == ["get_labs"]
    assert fake.consumed == 5


def test_route_to_evidence_retriever_degrades_when_tool_absent(
    real_llm_env, stub_tools, fake_client_factory, ctx, monkeypatch
):
    """If search_guidelines ever vanishes from TOOL_FUNCS (registry regression,
    partial deploy), the worker must degrade with ok=False and hand back — no
    crash, and NO worker LLM call is made."""
    monkeypatch.delitem(stub_tools, "search_guidelines", raising=False)
    fake = fake_client_factory([
        fake_resp(tool_use("route_to_evidence_retriever",
                           {"reason": "guideline target question"}, id="tu_s1")),
        fake_resp(tool_use("finalize", {"reason": "proceed without guidelines"}, id="tu_s2")),
        submit_briefing_resp(),
    ])

    resp, _ = _run(ctx, "what should the A1c target be?")

    routes = [h.route for h in resp.handoffs]
    assert routes == [
        "route_to_evidence_retriever", "worker_return", "finalize",
        "critique", "critic_passed",
    ]
    assert resp.handoffs[1].payload_summary.startswith("degraded")
    assert "0 of 1" in resp.handoffs[1].payload_summary
    # Degraded worker consumed zero LLM calls: 2 supervisor + 1 answerer only.
    assert fake.consumed == 3


def test_finalize_route_direct(real_llm_env, stub_tools, fake_client_factory, ctx):
    """finalize on the first decision → single handoff straight to the answerer."""
    fake = fake_client_factory([
        fake_resp(tool_use("finalize", {"reason": "plain chart question"}, id="tu_s1")),
        submit_briefing_resp(),
    ])

    resp, _ = _run(ctx, "brief me")

    assert [h.route for h in resp.handoffs] == ["finalize", "critique", "critic_passed"]
    assert (resp.handoffs[0].from_node, resp.handoffs[0].to_node) == ("supervisor", "answerer")
    assert fake.consumed == 2


def test_unknown_routing_tool_logs_routing_error_and_recovers(
    real_llm_env, stub_tools, fake_client_factory, log_capture, ctx
):
    """An out-of-vocabulary routing tool → routing_error event, tool_result
    (is_error) back to the supervisor, loop continues to a clean finalize."""
    fake = fake_client_factory([
        fake_resp(tool_use("route_to_mars", {"reason": "model drift"}, id="tu_x")),
        fake_resp(tool_use("finalize", {"reason": "recovered"}, id="tu_s2")),
        submit_briefing_resp(),
    ])

    resp, _ = _run(ctx, "brief me")

    # no handoff for the bad route
    assert [h.route for h in resp.handoffs] == ["finalize", "critique", "critic_passed"]
    assert fake.consumed == 3
    events = [json.loads(line) for line in log_capture.getvalue().splitlines() if line.strip()]
    routing_errors = [e for e in events if e.get("msg") == "routing_error"]
    assert routing_errors, "expected a routing_error event"
    assert routing_errors[0].get("error") == "unknown_route"
    assert routing_errors[0].get("tool") == "route_to_mars"


def test_routing_tools_expose_no_scoping_args():
    """SECURITY (injection guard parity): the supervisor's routing tools, like
    every model-facing schema, must expose no patient/user identifier."""
    from app.graph.supervisor import ROUTING_TOOLS

    forbidden = {"pid", "user_id", "patient_id", "userid", "patientid"}
    assert len(ROUTING_TOOLS) == 3
    for tool in ROUTING_TOOLS:
        props = set(tool["input_schema"].get("properties", {}))
        assert not (props & forbidden), f"routing tool {tool['name']!r} leaks scoping args"
