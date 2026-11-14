"""Regression: degraded guideline retrieval must be a REPORTED state, not silent.

W2_ARCHITECTURE §9.7: "Degradation is a first-class reported state ... never a
silent one." Before this fix, `_run_tool('search_guidelines')` registered
`res.chunks` and dropped `GuidelineSearchResult.degraded/provider/reranker`, so a
mid-turn Cohere outage (dense-embed fail → BM25-only, or reranker fail → Noop)
produced `AgentResponse.degraded=False`, no note, and no TurnObserver flag — a
grader/physician could not tell retrieval had fallen back.

Failure modes guarded:
- (invariant 9 / honesty) a degraded retrieval flag reaches obs + AgentResponse;
- (regression) a HEALTHY retrieval does not false-positive as degraded;
- (PHI) only provider/reranker enum names + a boolean surface — never values,
  and the new obs field is on the log allowlist.
"""

from __future__ import annotations

from app.contracts import EvidenceChunk, GuidelineRef, GuidelineSearchResult
from app.correlation import _ALLOWED_LOG_KEYS, set_correlation_id
from app.graph.workers import _Turn, _finalize, _run_tool
from app.observability import TurnObserver
from app.tools import TOOL_FUNCS, ToolContext


def _result(*, degraded: bool, provider: str, reranker: str) -> GuidelineSearchResult:
    chunk = EvidenceChunk(
        ref=GuidelineRef(
            doc_id="metformin-egfr", chunk_id="c0", section="Dosing", label="Metformin/eGFR"
        ),
        quote="metformin is contraindicated when egfr is below 30",
        score=0.9,
        source_org="ADA",
        source_url="https://example.org/g",
    )
    return GuidelineSearchResult(
        chunks=[chunk], provider=provider, reranker=reranker, degraded=degraded
    )


def _drive(monkeypatch, result: GuidelineSearchResult):
    set_correlation_id("degr-cid")
    monkeypatch.setitem(TOOL_FUNCS, "search_guidelines", lambda ctx, query="": result)
    turn = _Turn()
    obs = TurnObserver(101, "message")
    ctx = ToolContext("degr-cid", 1, 101)
    items = _run_tool("search_guidelines", {"query": "metformin egfr"}, ctx, turn)
    resp = _finalize(
        {"claims": [{"text": items[0]["data"], "handles": [items[0]["handle"]]}]}, turn, obs
    )
    return turn, obs, resp


def test_degraded_retrieval_flag_reaches_obs_and_response(monkeypatch):
    turn, obs, resp = _drive(
        monkeypatch, _result(degraded=True, provider="bm25", reranker="noop")
    )
    # captured on the turn (not dropped by _run_tool)
    assert turn.retrieval_degraded is True
    assert turn.retrieval_provider == "bm25"
    assert turn.retrieval_reranker == "noop"
    # observability sees it, PHI-free
    assert obs.retrieval_degraded is True
    assert obs.retrieval_provider == "bm25"
    assert obs.rerank_provider == "noop"
    meta = obs.metadata()
    assert meta["retrieval_degraded"] is True
    assert meta["provider"] == "bm25" and meta["rerank_provider"] == "noop"
    # AgentResponse surfaces it — first-class, not silent
    assert resp.degraded is True
    assert resp.notes and "degraded" in resp.notes.lower()
    assert "bm25" in resp.notes and "noop" in resp.notes


def test_healthy_retrieval_is_not_flagged_degraded(monkeypatch):
    """Control: a healthy retrieval must not false-positive as degraded."""
    turn, obs, resp = _drive(
        monkeypatch, _result(degraded=False, provider="cohere", reranker="cohere")
    )
    assert turn.retrieval_degraded is False
    assert obs.retrieval_degraded is False
    assert resp.degraded is False
    assert not (resp.notes and "degraded" in resp.notes.lower())


def test_retrieval_degraded_key_is_phi_allowlisted():
    """The new per-turn flag is on the log allowlist (bool), so it survives
    _sanitize into the structured log / LangSmith metadata."""
    assert "retrieval_degraded" in _ALLOWED_LOG_KEYS
    assert {"provider", "rerank_provider"} <= _ALLOWED_LOG_KEYS
