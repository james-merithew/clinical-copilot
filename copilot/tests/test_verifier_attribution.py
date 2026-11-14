"""Hermetic unit tests for the deterministic Verifier's attribution + field
grounding (app.verifier.verify over app.evidence.EvidenceSet).

These exercise the HARD SOURCE-ATTRIBUTION invariant (CLAUDE.md #4): a factual
claim reaches the user only if it binds to a real evidence handle issued this
turn AND its salient tokens (dose/lab values, keywords) appear in that cited
evidence. Anything else is QUARANTINED (fail-closed), never shown as fact.

No DB, no LLM: evidence is built in-code from fake SourceRefs.
"""

from __future__ import annotations

from app.contracts import LabResult, Medication, SourceRef
from app.evidence import EvidenceSet
from app.verifier import RawClaim, verify


def _empty_evidence_and_lists():
    """A fresh EvidenceSet plus empty structured lists for the constraint scan."""
    return EvidenceSet(), [], [], []


def _med_evidence():
    """EvidenceSet seeded with one grounded Metformin med + its handle."""
    ev = EvidenceSet()
    med = Medication(
        source=SourceRef(table="prescriptions", primary_key=147, label="Rx Metformin"),
        name="Metformin",
        dose="1000 mg",
        status="active",
    )
    handle = ev.add(med.HANDLE_PREFIX, med.source, med.evidence_text())
    return ev, med, handle


def test_claim_with_no_handles_is_quarantined():
    """INVARIANT (attribution/provenance): a claim citing ZERO evidence handles
    is unattributable and must fail closed — guards against the model asserting
    facts with no source at all."""
    ev, meds, algs, labs = _empty_evidence_and_lists()
    ev.add("med", SourceRef(table="prescriptions", primary_key=1, label="x"), "Metformin 1000 mg active")
    res = verify([RawClaim(text="Patient takes Metformin", handles=[])], ev, meds, algs, labs)
    assert res.verified_claims == []
    assert len(res.quarantined_claims) == 1
    assert not res.passed


def test_claim_with_bogus_handle_is_quarantined():
    """INVARIANT (provenance): a handle that was never issued this turn is a
    fabricated citation and must fail closed — guards against the model inventing
    a plausible-looking handle string."""
    ev, meds, algs, labs = _empty_evidence_and_lists()
    ev.add("med", SourceRef(table="prescriptions", primary_key=1, label="x"), "Metformin 1000 mg active")
    res = verify([RawClaim(text="Patient takes Metformin", handles=["med:99"])], ev, meds, algs, labs)
    assert res.verified_claims == []
    assert len(res.quarantined_claims) == 1


def test_claim_value_not_in_evidence_is_quarantined():
    """INVARIANT (value grounding): a dose value in the claim ('750 mg') that is
    absent from the cited evidence (which says 1000 mg) must fail closed — guards
    against dose/lab-value hallucination even when the right row is cited."""
    ev, _med, handle = _med_evidence()
    _, meds, algs, labs = _empty_evidence_and_lists()
    res = verify([RawClaim(text="Metformin 750 mg daily", handles=[handle])], ev, meds, algs, labs)
    assert res.verified_claims == []
    assert len(res.quarantined_claims) == 1


def test_claim_citing_wrong_row_is_quarantined():
    """INVARIANT (content overlap): a claim about lisinopril that cites the
    metformin row shares no content keyword with its evidence and must fail
    closed — guards against wholesale mis-citation (right handle exists, wrong
    subject)."""
    ev, _med, handle = _med_evidence()
    _, meds, algs, labs = _empty_evidence_and_lists()
    res = verify([RawClaim(text="Patient started lisinopril", handles=[handle])], ev, meds, algs, labs)
    assert res.verified_claims == []
    assert len(res.quarantined_claims) == 1


def test_correctly_grounded_claim_is_verified():
    """INVARIANT (positive path): a claim whose subject and value both match the
    cited evidence must pass — guards against an over-strict verifier that would
    quarantine legitimate, well-attributed claims (false-positive regression)."""
    ev, _med, handle = _med_evidence()
    _, meds, algs, labs = _empty_evidence_and_lists()
    res = verify([RawClaim(text="Metformin 1000 mg is active", handles=[handle])], ev, meds, algs, labs)
    assert len(res.verified_claims) == 1
    assert res.quarantined_claims == []
    assert res.passed
    # The verified claim carries the real source PK it was bound to.
    assert res.verified_claims[0].source_refs[0].primary_key == 147


def test_lab_value_grounding_positive_and_negative():
    """INVARIANT (value grounding, labs): the A1c value 8.2 verifies against its
    own row but a fabricated 9.9 is quarantined — guards lab-value integrity, the
    other hallucination-prone numeric surface besides doses."""
    ev = EvidenceSet()
    lab = LabResult(
        source=SourceRef(table="procedure_result", primary_key=500, label="A1c 2026-05-01"),
        name="Hemoglobin A1c",
        value="8.2",
        units="%",
        range="4.0-5.6",
        abnormal=True,
        date="2026-05-01",
    )
    handle = ev.add(lab.HANDLE_PREFIX, lab.source, lab.evidence_text())
    good = verify([RawClaim(text="Hemoglobin A1c is 8.2 %", handles=[handle])], ev, [], [], [])
    assert len(good.verified_claims) == 1 and good.passed
    bad = verify([RawClaim(text="Hemoglobin A1c is 9.9 %", handles=[handle])], ev, [], [], [])
    assert bad.quarantined_claims and not bad.verified_claims
