"""Week 1 regression + deterministic mock routing (W2 D5).

Failure modes guarded:
- (regression) mock mode for a PLAIN question returns the exact Week 1
  briefing — byte-compatible at the serialized-contract level, handoffs empty
  — so /chat, k6 load runs, and every Week 1 consumer are unchanged;
- (invariant) mock keyword routing is deterministic: document words → intake
  route, guideline words → evidence route — hermetic tests and load runs can
  exercise the graph without tokens.
"""

from __future__ import annotations

import pytest

from app.contracts import AgentResponse, Claim, SourceRef
from app.correlation import set_correlation_id
from app.tools import ToolContext


@pytest.fixture
def ctx():
    set_correlation_id("mock-regr-cid")
    return ToolContext("mock-regr-cid", 1, 101)


def _run_mock(ctx, user_text):
    from app.agent import run_turn
    from app.observability import TurnObserver

    obs = TurnObserver(101, "message")
    return run_turn(ctx, user_text, [], obs), obs


def test_mock_plain_question_byte_compatible_with_week1(mock_llm_env, stub_tools, ctx):
    """A plain question in mock mode must serialize IDENTICALLY to the Week 1
    deterministic briefing: same claims (tool order), same notes, not degraded.
    Frozen against the stub tools' evidence_text output. Week 2 critic: the
    only additive deltas are the `critic` report and the critique/critic_passed
    trail — everything else stays byte-compatible."""
    resp, obs = _run_mock(ctx, "Brief me on this patient for today's visit.")

    expected = AgentResponse(
        correlation_id=resp.correlation_id,  # dynamic by design (Week 1 too)
        briefing=[
            Claim(
                text="2026-07-01 ed encounter reason=chest pain provider=Dr. A",
                source_refs=[SourceRef(table="form_encounter", primary_key=7, label="ED visit")],
            ),
            Claim(
                text="lisinopril 10 mg active",
                source_refs=[SourceRef(table="prescriptions", primary_key=3, label="lisinopril")],
            ),
            Claim(
                text="hypertension status=active onset=",
                source_refs=[SourceRef(table="lists", primary_key=5, label="hypertension")],
            ),
            Claim(
                text="Hemoglobin A1c 8.7 % range= 2026-07-01",
                source_refs=[SourceRef(table="procedure_result", primary_key=11, label="A1c")],
            ),
            Claim(
                text="Sodium 142 mmol/L range= 2026-07-01",
                source_refs=[SourceRef(table="procedure_result", primary_key=12, label="Sodium")],
            ),
        ],
        notes="Mock LLM briefing (load-test mode).",
    )
    assert resp.model_dump(exclude={"handoffs", "critic"}) == expected.model_dump(
        exclude={"handoffs", "critic"}
    )
    # Plain question: no supervisor routing — only the critic's own trail.
    assert [h.route for h in resp.handoffs] == ["critique", "critic_passed"]
    assert resp.critic is not None and resp.critic.reviewed is True
    assert obs.tool_calls == 4  # Week 1 mock accounting preserved


def test_mock_doc_keywords_route_to_intake_extractor(mock_llm_env, stub_tools, ctx):
    """Document words → intake route; the worker registers evidence through
    existing read tools into the SHARED evidence set before the briefing."""
    resp, _ = _run_mock(ctx, "summarize the uploaded intake form")

    assert [h.route for h in resp.handoffs] == [
        "route_to_intake_extractor", "worker_return", "finalize",
        "critique", "critic_passed",
    ]
    assert resp.handoffs[0].to_node == "intake_extractor"
    # Worker gathered labs (its only registered subset tool) as lab:0/lab:1.
    assert resp.handoffs[1].payload_summary.startswith("ok: 2 evidence handle(s)")
    # The briefing itself still verifies — mock path remains fail-closed.
    assert not resp.degraded
    assert resp.quarantined_count == 0


def test_mock_evidence_keywords_route_to_evidence_retriever(mock_llm_env, stub_tools, ctx):
    """Guideline words → evidence route; in mock mode the real search_guidelines
    tool runs hermetically (BM25 + local TF-IDF over the repo corpus, no keys)
    and the worker hands back gl: handles — the turn still completes with the
    full mock briefing.

    Week 2 fix (mock RAG answer path): the mock briefing now CITES the gathered
    `gl:` chunks, so the verifier partitions them into `guideline_evidence` —
    the RAG answer path is demonstrable in hermetic mock/demo mode. The 5 patient
    briefing claims and the not-degraded/fail-closed guarantees are unchanged."""
    resp, _ = _run_mock(ctx, "what should her A1c target be per screening guidelines?")

    assert [h.route for h in resp.handoffs] == [
        "route_to_evidence_retriever", "worker_return", "finalize",
        "critique", "critic_passed",
    ]
    assert resp.handoffs[0].to_node == "evidence_retriever"
    # Retrieval over the real corpus surfaced guideline chunks as gl: handles.
    assert resp.handoffs[1].payload_summary.startswith("ok")
    assert "gl:" in resp.handoffs[1].payload_summary
    # The worker gap/success never poisons the briefing.
    assert not resp.degraded
    assert len(resp.briefing) == 5
    # Mock RAG answer path: the gathered guideline chunks are now surfaced as
    # verified guideline_evidence (separate from the patient briefing), each
    # citing ONLY gl: handles (the verifier's separation rule).
    assert len(resp.guideline_evidence) >= 1
    for claim in resp.guideline_evidence:
        assert claim.source_refs
        assert all(getattr(r, "source_type", "") == "guideline" for r in claim.source_refs)
    # Reference knowledge never leaks into the patient briefing.
    for claim in resp.briefing:
        assert not any(getattr(r, "source_type", "") == "guideline" for r in claim.source_refs)
