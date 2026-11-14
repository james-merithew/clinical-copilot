"""Per-node model tiers (latency tuning, early-submission follow-up).

The measured live run (copilot/load/latency_report.md, 2026-07-16) put the
supervisor at 3.5s p50 and the worker sub-loop at 11.3s p50 on the same
Sonnet-class model the answerer uses — but routing is a forced choice among
3 tools and worker calls are bounded tool loops; neither needs answerer-class
reasoning (the deterministic verifier is the quality guarantee regardless).

Failure modes guarded:
- (regression) a node silently ignoring its configured model tier — the
  latency fix would evaporate without any test going red;
- (boundary) an unset override must fall back to the main model, so existing
  deployments see zero behavior change;
- (invariant) the tier split is per-node: overriding one node never bleeds
  into another.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.correlation import set_correlation_id
from app.tools import ToolContext
from graph_utils import fake_resp, submit_briefing_resp, text_block, tool_use


@pytest.fixture
def ctx():
    set_correlation_id("model-tier-test-cid")
    return ToolContext("model-tier-test-cid", 1, 101)


def _script():
    """supervisor -> intake worker (2 calls) -> supervisor -> answerer."""
    return [
        fake_resp(tool_use("route_to_intake_extractor", {"reason": "docs"}, id="tu_s1")),
        fake_resp(tool_use("get_labs", {}, id="tu_w1")),
        fake_resp(text_block("enough gathered")),
        fake_resp(tool_use("finalize", {"reason": "evidence in hand"}, id="tu_s2")),
        submit_briefing_resp(),
    ]


def _run(ctx):
    from app.agent import run_turn
    from app.observability import TurnObserver

    return run_turn(ctx, "summarize the uploaded intake form", [], TurnObserver(101, "message"))


def test_default_every_node_uses_main_model(
    real_llm_env, stub_tools, fake_client_factory, ctx, monkeypatch
):
    """No overrides set -> zero behavior change: all calls on anthropic_model.
    (setenv to empty, not delenv: a developer .env with tiers set would
    otherwise leak in — env vars outrank dotenv.)"""
    for k in ("COPILOT_SUPERVISOR_MODEL", "COPILOT_WORKER_MODEL", "COPILOT_ANSWERER_MODEL"):
        monkeypatch.setenv(k, "")
    get_settings.cache_clear()
    try:
        fake = fake_client_factory(_script())
        _run(ctx)
        main = get_settings().anthropic_model
        assert [c["model"] for c in fake.calls] == [main] * 5
    finally:
        get_settings.cache_clear()


def test_per_node_model_overrides(
    real_llm_env, stub_tools, fake_client_factory, ctx, monkeypatch
):
    monkeypatch.setenv("COPILOT_SUPERVISOR_MODEL", "fast-router")
    monkeypatch.setenv("COPILOT_WORKER_MODEL", "fast-worker")
    monkeypatch.setenv("COPILOT_ANSWERER_MODEL", "quality-answerer")
    get_settings.cache_clear()
    try:
        fake = fake_client_factory(_script())
        _run(ctx)
        assert [c["model"] for c in fake.calls] == [
            "fast-router",   # supervisor decision 1
            "fast-worker",   # worker tool call
            "fast-worker",   # worker wrap-up
            "fast-router",   # supervisor decision 2
            "quality-answerer",
        ]
    finally:
        get_settings.cache_clear()


def test_partial_override_falls_back_per_node(
    real_llm_env, stub_tools, fake_client_factory, ctx, monkeypatch
):
    """Only the supervisor overridden -> workers/answerer stay on the main model."""
    monkeypatch.setenv("COPILOT_SUPERVISOR_MODEL", "fast-router")
    monkeypatch.setenv("COPILOT_WORKER_MODEL", "")
    monkeypatch.setenv("COPILOT_ANSWERER_MODEL", "")
    get_settings.cache_clear()
    try:
        main = get_settings().anthropic_model
        fake = fake_client_factory(_script())
        _run(ctx)
        assert [c["model"] for c in fake.calls] == [
            "fast-router", main, main, "fast-router", main,
        ]
    finally:
        get_settings.cache_clear()
