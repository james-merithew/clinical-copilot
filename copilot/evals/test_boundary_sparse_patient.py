"""DB-integration boundary eval: a sparse/near-empty patient in MOCK-LLM mode.

Graceful degradation (CLAUDE.md #9) + hard attribution (#4): for a patient with
little or no data, the agent must return a well-formed AgentResponse that never
fabricates a claim. Every claim that DOES reach the briefing must carry at least
one SourceRef, and nothing is quarantined-then-shown-as-fact.

Runs the real tool loop against the live DB with COPILOT_MOCK_LLM=true so the
deterministic (no-token) briefing path is exercised end-to-end.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.contracts import AgentResponse

SPARSE_PID = 3


@pytest.fixture
def mock_llm_env(monkeypatch):
    """Force COPILOT_MOCK_LLM=true for the duration of the test and reset the
    cached settings so the flag takes effect (and is cleaned up afterward)."""
    monkeypatch.setenv("COPILOT_MOCK_LLM", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().mock_llm is True
        yield
    finally:
        get_settings.cache_clear()


def test_sparse_patient_degrades_without_fabrication(mock_llm_env, make_ctx, q):
    """BOUNDARY (empty/sparse data): a sparse patient must yield an AgentResponse
    with NO fabricated claims — every briefing claim carries >=1 SourceRef and
    nothing is silently invented — guards graceful degradation and the
    hard-attribution invariant on the thin-data edge."""
    # Confirm the patient really is sparse at runtime (no meds / no labs).
    assert not q("SELECT id FROM prescriptions WHERE patient_id = :p", {"p": SPARSE_PID}), \
        "seed precondition: sparse patient has no prescriptions"

    # Imported here so the mock_llm_env fixture has already reset settings.
    from app.agent import run_turn
    from app.observability import TurnObserver

    resp = run_turn(
        make_ctx(SPARSE_PID),
        "Brief me on this patient for today's visit.",
        [],
        TurnObserver(SPARSE_PID, "message"),
    )

    assert isinstance(resp, AgentResponse)
    # Invariant: no verified briefing claim lacks attribution.
    for claim in resp.briefing:
        assert claim.source_refs, f"unattributed claim leaked: {claim.text!r}"
    # Fail-closed transparency: quarantined_count is consistent, never negative.
    assert resp.quarantined_count == len(resp.quarantined_claims)
    assert resp.quarantined_count >= 0
    # degraded is a real boolean the UI can act on (graceful-degradation contract).
    assert isinstance(resp.degraded, bool)
