"""HIPAA invariant (integration): a full turn emits NO PHI to the log sink.

Runs a real (mock-LLM) turn over Showcase B — whose data contains names and drug
names — while capturing everything the JSON log formatter emits, and asserts none
of those PHI values appear. Guards the 'PHI never in logs' claim end-to-end.
"""

import io
import logging

import pytest


@pytest.fixture
def _mock_env(monkeypatch):
    monkeypatch.setenv("COPILOT_MOCK_LLM", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_no_phi_in_turn_logs(_mock_env):
    from app.agent import run_turn
    from app.correlation import _JsonFormatter, set_correlation_id
    from app.observability import TurnObserver
    from app.tools import ToolContext

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    set_correlation_id("phi-test-cid")

    try:
        obs = TurnObserver(102, "message")
        run_turn(ToolContext("phi-test-cid", 1, 102), "anything to watch for?", [], obs)
        obs.finish()
    finally:
        root.removeHandler(handler)

    logs = buf.getvalue()
    assert logs, "expected some log output"
    for phi in ("Marcus", "Delgado", "Eleanor", "Whitfield",
                "Amoxicillin", "Penicillin", "Warfarin", "Metformin", "Ibuprofen"):
        assert phi not in logs, f"PHI '{phi}' leaked into logs"
    # sanity: the correlation id and structured event names DID make it through
    assert "phi-test-cid" in logs
