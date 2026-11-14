"""_finalize must never raise on malformed model output (untrusted planner).

Failure mode guarded: the model's submit_briefing `claims` came back as a list
of bare strings (or `brief` was not a dict). The Week-1 code assumed each claim
was a {text, handles} dict and called `.get()` on it — an AttributeError that
escaped the graph and surfaced as an HTTP 500 in production. Malformed claims
must be dropped, the turn degrading gracefully, never crashing.
"""

from __future__ import annotations

from app.correlation import set_correlation_id
from app.graph.workers import _Turn, _finalize
from app.observability import TurnObserver


def _fin(brief):
    set_correlation_id("fin-robust")
    return _finalize(brief, _Turn(), TurnObserver(101, "message"))


def test_claims_as_bare_strings_do_not_crash():
    resp = _fin({"claims": ["A1c target is below 7%", "Screen annually"]})
    assert resp.briefing == []  # malformed claims dropped, not verified
    assert resp.correlation_id


def test_mixed_valid_and_string_claims_keeps_valid_shape():
    # a valid-shaped claim with no real handle still won't verify, but must not
    # crash alongside a bare-string claim.
    resp = _fin({"claims": [{"text": "x", "handles": []}, "bare string claim"]})
    assert isinstance(resp.briefing, list)


def test_non_dict_brief_does_not_crash():
    resp = _fin("not-a-dict")  # tu.input could be odd
    assert resp.briefing == []


def test_claims_missing_entirely():
    resp = _fin({"notes": "no claims key"})
    assert resp.briefing == []
