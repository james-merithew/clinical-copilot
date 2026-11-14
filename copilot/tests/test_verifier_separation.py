"""Hermetic tests for the verifier's guideline/patient SEPARATION RULE
(W2_ARCHITECTURE §6.3 / plan D4): after standard verification, claims citing
ONLY `gl:` guideline handles go to `guideline_evidence`; a claim with zero
patient-record handles can NEVER appear in the patient briefing. Mixed claims
(patient + guideline refs) stay in the briefing with values grounded against
the UNION of cited evidence text.

Failure modes guarded:
- guideline text laundering into patient facts (the core Week 2 trust risk:
  "ADA says <7%" rendered as if it were this patient's chart);
- a fabricated gl: handle passing provenance;
- mixed-claim numbers grounding against the wrong evidence side;
- regression of Week 1 behavior for patient-only turns.

No DB, no LLM — evidence built in-code, same style as test_verifier_attribution.
"""

from __future__ import annotations

from app.contracts import EvidenceChunk, GuidelineRef, LabResult, SourceRef
from app.evidence import EvidenceSet
from app.verifier import RawClaim, verify


def _gl_chunk(quote: str, chunk_id: str = "ada-soc-glycemic#glycemic-targets:0") -> EvidenceChunk:
    doc_id = chunk_id.split("#", 1)[0]
    return EvidenceChunk(
        ref=GuidelineRef(
            doc_id=doc_id,
            chunk_id=chunk_id,
            section="Glycemic targets",
            label="ADA SoC — glycemic targets",
        ),
        quote=quote,
        score=1.0,
        source_org="ADA",
        source_url="https://example.org/ada",
    )


def _evidence_with_lab_and_guideline():
    """EvidenceSet holding one patient lab row (A1c 9.2) and one guideline
    chunk (target below 7 %), returning (ev, lab_handle, gl_handle)."""
    ev = EvidenceSet()
    lab = LabResult(
        source=SourceRef(table="procedure_result", primary_key=500, label="A1c 2026-06-01"),
        name="Hemoglobin A1c",
        value="9.2",
        units="%",
        abnormal=True,
        date="2026-06-01",
    )
    lh = ev.add(lab.HANDLE_PREFIX, lab.source, lab.evidence_text())
    chunk = _gl_chunk(
        "Our practice targets hemoglobin A1c below 7 % for most nonpregnant adults with diabetes."
    )
    gh = ev.add(chunk.HANDLE_PREFIX, chunk.ref, chunk.evidence_text())
    return ev, lh, gh


def test_guideline_only_claim_goes_to_guideline_evidence_never_briefing():
    """SEPARATION RULE: a verified claim citing only gl: handles lands in
    guideline_evidence — and NOT in verified_claims (the briefing feed)."""
    ev, _lh, gh = _evidence_with_lab_and_guideline()
    res = verify(
        [RawClaim(text="Practice guidance targets hemoglobin A1c below 7 % for most adults", handles=[gh])],
        ev, [], [], [],
    )
    assert res.verified_claims == []
    assert len(res.guideline_evidence) == 1
    assert res.quarantined_claims == []
    assert res.passed
    ref = res.guideline_evidence[0].source_refs[0]
    assert ref.source_type == "guideline"
    assert ref.chunk_id == "ada-soc-glycemic#glycemic-targets:0"


def test_mixed_claim_stays_in_briefing_with_both_refs():
    """SEPARATION RULE (mixed allowed): a claim citing a patient lab AND a
    guideline chunk stays in the briefing, carrying both source refs."""
    ev, lh, gh = _evidence_with_lab_and_guideline()
    res = verify(
        [RawClaim(
            text="Hemoglobin A1c 9.2 % exceeds the practice target of below 7 % for adults",
            handles=[lh, gh],
        )],
        ev, [], [], [],
    )
    assert len(res.verified_claims) == 1
    assert res.guideline_evidence == []
    types = sorted(r.source_type for r in res.verified_claims[0].source_refs)
    assert types == ["guideline", "patient_record"]


def test_mixed_claim_values_ground_against_union_of_cited_evidence():
    """VALUE GROUNDING (union): 9.2 comes from the lab row, 7 % from the
    guideline chunk — the claim verifies only because grounding treats the
    union of cited evidence text; a fabricated 9.9 still fails closed."""
    ev, lh, gh = _evidence_with_lab_and_guideline()
    ok = verify(
        [RawClaim(text="Hemoglobin A1c 9.2 % is above the practice goal of 7 %", handles=[lh, gh])],
        ev, [], [], [],
    )
    assert len(ok.verified_claims) == 1

    bad = verify(
        [RawClaim(text="Hemoglobin A1c 9.9 % is above the practice goal of 7 %", handles=[lh, gh])],
        ev, [], [], [],
    )
    assert bad.verified_claims == [] and bad.guideline_evidence == []
    assert len(bad.quarantined_claims) == 1


def test_fabricated_gl_handle_is_quarantined():
    """PROVENANCE (fail-closed): a gl: handle never issued this turn is a
    fabricated citation → quarantined, exactly like a bogus med: handle."""
    ev, _lh, _gh = _evidence_with_lab_and_guideline()
    res = verify(
        [RawClaim(text="Guidelines recommend aggressive treatment", handles=["gl:9"])],
        ev, [], [], [],
    )
    assert res.verified_claims == [] and res.guideline_evidence == []
    assert len(res.quarantined_claims) == 1
    assert not res.passed


def test_guideline_value_not_in_chunk_is_quarantined():
    """VALUE GROUNDING (gl side): a guideline-only claim whose number is absent
    from the cited chunk fails closed — retrieval does not license invention."""
    ev, _lh, gh = _evidence_with_lab_and_guideline()
    res = verify(
        [RawClaim(text="Practice guidance targets hemoglobin A1c below 6.5 % for adults", handles=[gh])],
        ev, [], [], [],
    )
    assert res.guideline_evidence == [] and res.verified_claims == []
    assert len(res.quarantined_claims) == 1


def test_patient_only_turns_are_unchanged():
    """WEEK 1 REGRESSION: with no guideline evidence in the turn, verify()
    behaves exactly as before — all verified claims in the briefing feed,
    guideline_evidence empty."""
    ev = EvidenceSet()
    lab = LabResult(
        source=SourceRef(table="procedure_result", primary_key=7, label="A1c"),
        name="Hemoglobin A1c", value="8.2", units="%",
    )
    h = ev.add(lab.HANDLE_PREFIX, lab.source, lab.evidence_text())
    res = verify([RawClaim(text="Hemoglobin A1c is 8.2 %", handles=[h])], ev, [], [], [])
    assert len(res.verified_claims) == 1
    assert res.guideline_evidence == []
    assert res.passed
