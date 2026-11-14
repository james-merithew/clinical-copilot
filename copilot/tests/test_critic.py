"""Deterministic critic node (W2 extension — the graph's 4th node).

Failure modes guarded:
- (invariant) a treatment/order recommendation ("Consider stopping lisinopril")
  reaching the physician through the patient channel or an Action Chip — the
  read-only invariant (#3) enforced on CONTENT, not just on tool capability;
- (invariant) an uncited claim reaching the verifier as anything but a
  quarantine candidate — the critic rejects it with `critic_rejected` and the
  quarantine merge keeps wire-level count parity with the Week 2 behavior;
- (regression, standing guard) the unsafe-action pattern false-positiving on
  legitimate clinical prose: every stub evidence text, every fixture-patient
  string, and every committed guideline-corpus chunk must pass the raw pattern
  — the corpus is full of directive guideline language ("start a statin",
  "Stop metformin") that must never be stripped from the guideline channel;
- (invariant/PHI) the critic logs counts only — never claim text or drug names.
"""

from __future__ import annotations

import json

import pytest

from app.contracts import SourceRef
from app.correlation import set_correlation_id
from app.critic import critique, is_unsafe_action
from app.graph.handoff import HandoffLog
from app.graph.workers import _Turn, _finalize
from app.observability import TurnObserver
from app.verifier import RawClaim

# ---- Check 2: unsafe-action pattern -------------------------------------------

UNSAFE_POSITIVES = [
    "Consider stopping lisinopril",
    "You should increase the metformin dose",
    "Order a CBC today",
    "Recommend switching to losartan",
    "Please refill oxycodone",
    "Order a CBC now",  # chip-label shape (imperative, no recommend frame)
]


@pytest.mark.parametrize("text", UNSAFE_POSITIVES)
def test_unsafe_action_positives(text):
    assert is_unsafe_action(text), f"must flag: {text!r}"


SAFE_NEGATIVES = [
    # Past-tense / stative chart facts — the VERIFIER's territory (ct-010:
    # 'Prescribed apixaban' with a fabricated handle must be QUARANTINED by
    # verify(), which requires it to pass the critic untouched).
    "Prescribed apixaban 5 mg",
    "Started lisinopril 10 mg on 2025-11-15",
    "Warfarin 5 mg active",
    "Metformin dose is 1000 mg daily",
    "A1c improved to 6.9 % last month",
]


@pytest.mark.parametrize("text", SAFE_NEGATIVES)
def test_unsafe_action_negatives(text):
    assert not is_unsafe_action(text), f"must NOT flag: {text!r}"


def _all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_strings(v)


def test_no_false_positive_on_stub_evidence_texts():
    """Standing guard: the deterministic stub tools' evidence texts (what the
    hermetic graph suite feeds through _finalize) never trip the screen."""
    from graph_utils import (
        stub_care_transitions,
        stub_labs,
        stub_medications,
        stub_problems_allergies,
    )

    records = list(stub_care_transitions(None).encounters)
    records += list(stub_medications(None).medications)
    pr = stub_problems_allergies(None)
    records += list(pr.problems) + list(pr.allergies)
    records += list(stub_labs(None).labs)
    assert records
    for rec in records:
        assert not is_unsafe_action(rec.evidence_text()), rec.evidence_text()


def test_no_false_positive_on_golden_fixture_patients():
    """Standing guard: every string in the golden patient fixtures (chart facts
    the mock briefing turns into claims verbatim) passes the screen."""
    from pathlib import Path

    fixtures = Path(__file__).resolve().parents[1] / "evals" / "golden" / "fixtures" / "patients.json"
    data = json.loads(fixtures.read_text(encoding="utf-8"))
    strings = list(_all_strings(data))
    assert len(strings) > 50
    for s in strings:
        assert not is_unsafe_action(s), f"fixture string false-positived: {s!r}"


def test_no_false_positive_on_committed_corpus_chunks():
    """Standing guard (the load-bearing one): guideline prose is FULL of
    directive language ('start a high-intensity statin', 'Stop metformin.',
    'Prescribe glucagon...'). The raw pattern must not match any committed
    chunk — otherwise a future scope regression (dropping the gl-only
    exemption) would silently strip real guideline evidence."""
    from app.rag.corpus import load_corpus

    chunks = load_corpus()
    assert len(chunks) >= 40
    for c in chunks:
        assert not is_unsafe_action(c.text), f"corpus chunk false-positived: {c.chunk_id}"


# ---- Check 1: uncited rejection + channel scoping -------------------------------


def test_critique_rejects_uncited_claim():
    res = critique([RawClaim(text="A1c is 8.4 % today", handles=[])], [])
    assert [rc.text for rc in res.rejected_uncited] == ["A1c is 8.4 % today"]
    assert res.surviving_claims == []
    assert res.report.reviewed is True
    assert res.report.rejected_uncited == 1
    assert res.report.stripped_unsafe == 0
    assert res.report.findings[0].kind == "uncited_claim"
    assert res.report.findings[0].disposition == "rejected_to_quarantine"
    assert res.report.model_review is None  # deterministic — no LLM pass, ever


def test_critique_passes_fabricated_handle_through_to_verifier():
    """Provenance is the VERIFIER's job — a cited-but-fake handle must pass the
    critic untouched (rf-004/009/010 depend on reaching verify())."""
    rc = RawClaim(text="Flu shot given 2025-10-01", handles=["fake:9"])
    res = critique([rc], [])
    assert res.surviving_claims == [rc]
    assert res.rejected_uncited == [] and res.stripped_unsafe == []
    assert res.report.rejected_uncited == 0 and res.report.stripped_unsafe == 0


def test_guideline_only_recommendation_survives():
    """Exemption (a): a claim citing ONLY gl: handles is reference knowledge —
    the corpus legitimately recommends starting/stopping drugs."""
    rc = RawClaim(text="Guidelines recommend initiating a statin for LDL-C 190 or higher",
                  handles=["gl:0", "gl:1"])
    res = critique([rc], [])
    assert res.surviving_claims == [rc]
    assert res.report.stripped_unsafe == 0 and res.report.findings == []


def test_mixed_patient_and_guideline_recommendation_stripped():
    """>=1 non-gl handle puts the claim in the PATIENT channel — a
    recommendation there is stripped (content problem, not evidence problem)."""
    rc = RawClaim(text="Consider stopping lisinopril", handles=["med:1", "gl:0"])
    res = critique([rc], [])
    assert res.surviving_claims == []
    assert res.stripped_unsafe == [rc]
    assert res.report.stripped_unsafe == 1
    f = res.report.findings[0]
    assert (f.kind, f.channel, f.disposition) == ("unsafe_action", "briefing", "stripped")


def test_order_chip_stripped_benign_chip_kept():
    chips = [
        {"label": "Order CBC", "intent": "care_gaps", "params": {}},
        {"label": "Show A1c trend", "intent": "lab_trend", "params": {}},
    ]
    res = critique([], chips)
    assert res.surviving_chips == [chips[1]]
    assert res.stripped_chips == [chips[0]]
    assert res.report.stripped_unsafe == 1
    f = res.report.findings[0]
    assert (f.kind, f.channel, f.disposition) == ("unsafe_action", "action_chip", "stripped")


# ---- _finalize integration -------------------------------------------------------


def _seeded_turn():
    """A turn whose evidence set holds one real med row (med:0 = lisinopril)."""
    turn = _Turn()
    src = SourceRef(table="prescriptions", primary_key=3, label="lisinopril")
    turn.evidence.add("med", src, "lisinopril 10 mg active")
    return turn


def _obs():
    return TurnObserver(101, "message")


def test_finalize_merges_critic_rejected_into_quarantine_with_count_parity():
    set_correlation_id("critic-fin-1")
    turn = _seeded_turn()
    obs = _obs()
    brief = {"claims": [
        {"text": "A1c is 8.4 % today", "handles": []},          # critic rejects
        {"text": "lisinopril 10 mg active", "handles": ["med:0"]},  # verifies
    ]}
    resp = _finalize(brief, turn, obs)
    assert len(resp.briefing) == 1
    assert resp.quarantined_count == 1
    assert len(resp.quarantined_claims) == 1
    assert resp.quarantined_claims[0].text == "A1c is 8.4 % today"
    assert resp.quarantined_claims[0].source_refs == []
    assert resp.critic is not None and resp.critic.reviewed is True
    assert resp.critic.rejected_uncited == 1
    assert obs.quarantined == 1  # obs parity with the wire count
    assert "critic" in obs.node_ms  # the critic ran under its own node span


def test_finalize_fabricated_handle_still_quarantined_by_verifier():
    """Follow-on for the pass-through: the fabricated-handle claim lands in
    quarantined_claims via verify(), exactly as before the critic existed."""
    set_correlation_id("critic-fin-2")
    resp = _finalize({"claims": [{"text": "Flu shot given", "handles": ["fake:9"]}]},
                     _Turn(), _obs())
    assert resp.briefing == []
    assert resp.quarantined_count == 1
    assert resp.quarantined_claims[0].text == "Flu shot given"
    assert resp.critic is not None and resp.critic.rejected_uncited == 0


def test_finalize_strip_appends_readonly_note():
    set_correlation_id("critic-fin-3")
    turn = _seeded_turn()
    resp = _finalize(
        {"claims": [{"text": "Consider stopping lisinopril", "handles": ["med:0"]}]},
        turn, _obs(),
    )
    assert resp.briefing == []  # the recommendation never reaches the physician
    assert resp.quarantined_count == 0  # stripped, NOT quarantined
    assert resp.critic is not None and resp.critic.stripped_unsafe == 1
    assert resp.notes and "read-only" in resp.notes
    assert "Removed 1 suggested order/medication action" in resp.notes


def test_finalize_clean_turn_notes_byte_identical_and_critic_passed_route():
    set_correlation_id("critic-fin-4")
    turn = _seeded_turn()
    hl = HandoffLog()
    resp = _finalize(
        {"claims": [{"text": "lisinopril 10 mg active", "handles": ["med:0"]}],
         "notes": "Chart reviewed."},
        turn, _obs(), hl=hl,
    )
    assert resp.notes == "Chart reviewed."  # byte-identical — nothing appended
    assert [h.route for h in hl.handoffs] == ["critique", "critic_passed"]
    assert (hl.handoffs[0].from_node, hl.handoffs[0].to_node) == ("answerer", "critic")
    assert (hl.handoffs[1].from_node, hl.handoffs[1].to_node) == ("critic", "answerer")
    assert resp.critic is not None
    assert resp.critic.rejected_uncited == 0 and resp.critic.stripped_unsafe == 0


def test_finalize_rejection_emits_critic_rejected_route():
    set_correlation_id("critic-fin-5")
    hl = HandoffLog()
    _finalize({"claims": [{"text": "uncited thing", "handles": []}]}, _Turn(), _obs(), hl=hl)
    assert [h.route for h in hl.handoffs] == ["critique", "critic_rejected"]


# ---- PHI discipline ---------------------------------------------------------------


def test_critic_logs_counts_only_never_claim_text(log_capture):
    """Sentinel drug name in an unsafe claim must never reach the log sink —
    the critic's single log line carries counts only."""
    set_correlation_id("critic-phi")
    turn = _Turn()
    src = SourceRef(table="prescriptions", primary_key=9, label="oxycodone")
    turn.evidence.add("med", src, "oxycodone 5 mg active")
    _finalize(
        {"claims": [{"text": "Please refill oxycodone 5 mg", "handles": ["med:0"]}],
         "action_chips": [{"label": "Order oxycodone refill", "intent": "med_detail"}]},
        turn, _obs(),
    )
    full_log = log_capture.getvalue()
    assert "oxycodone" not in full_log
    assert "refill" not in full_log.lower()
    events = [json.loads(ln) for ln in full_log.splitlines() if ln.strip()]
    critic_events = [e for e in events if e.get("msg") == "critic"]
    assert len(critic_events) == 1
    assert critic_events[0].get("rejected_uncited") == 0
    assert critic_events[0].get("stripped_unsafe") == 2  # claim + chip
