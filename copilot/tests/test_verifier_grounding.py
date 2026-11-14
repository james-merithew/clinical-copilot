"""Regression tests for two confirmed anti-invention grounding defects
(CLAUDE.md invariant #4 — hard source attribution, fail-closed).

Finding 2 (HIGH): bare-integer clinical values (no unit, no decimal) were
exempt from value grounding, so a fabricated integer lab value verified against
the real row it contradicts ('Sodium 138' vs a row that says 139). Counts
('3 medications') and dates/years stay exempt so legitimate summaries are not
false-quarantined.

Finding 3 (HIGH): value grounding checked the claim's numbers against the UNION
of ALL cited evidence, so a number present only in cited GUIDELINE text could
ground a value the claim asserts as the patient's ('Her A1c is 7.0' grounded by
an ADA target chunk). A patient value must ground against PATIENT evidence only;
guideline-only claims still ground on guideline text.

No DB, no LLM — evidence built in-code, same style as test_verifier_attribution.
"""

from __future__ import annotations

from app.contracts import EvidenceChunk, GuidelineRef, LabResult, Medication, SourceRef
from app.evidence import EvidenceSet
from app.verifier import RawClaim, verify


def _gl_chunk(quote: str, chunk_id: str = "ada-soc-glycemic#glycemic-targets:0") -> EvidenceChunk:
    return EvidenceChunk(
        ref=GuidelineRef(
            doc_id=chunk_id.split("#", 1)[0],
            chunk_id=chunk_id,
            section="Glycemic targets",
            label="ADA SoC — glycemic targets",
        ),
        quote=quote,
        score=1.0,
        source_org="ADA",
        source_url="https://example.org/ada",
    )


def _sodium_evidence():
    ev = EvidenceSet()
    lab = LabResult(
        source=SourceRef(table="procedure_result", primary_key=42, label="Na 2026-06-01"),
        name="Sodium",
        value="139",
        units="mmol/L",
        range="135-145",
        date="2026-06-01",
    )
    return ev, ev.add(lab.HANDLE_PREFIX, lab.source, lab.evidence_text())


# --- Finding 2: bare-integer value grounding -----------------------------------


def test_fabricated_bare_integer_lab_value_is_quarantined():
    """A bare integer with no unit/decimal ('138') that contradicts the single
    cited row (139) must fail closed — it is a clinical value, not a count."""
    ev, h = _sodium_evidence()
    res = verify([RawClaim(text="Sodium is 138", handles=[h])], ev, [], [], [])
    assert res.verified_claims == []
    assert len(res.quarantined_claims) == 1


def test_true_bare_integer_lab_value_verifies():
    """Control: the real integer value still verifies (no over-quarantine)."""
    ev, h = _sodium_evidence()
    res = verify([RawClaim(text="Sodium is 139", handles=[h])], ev, [], [], [])
    assert len(res.verified_claims) == 1
    assert res.quarantined_claims == []


def test_bare_integer_count_is_not_a_value():
    """PROTECTED FALSE-POSITIVE: '3 active medications' is a count, not a lab
    value — it must not require '3' to appear in evidence."""
    ev = EvidenceSet()
    med = Medication(
        source=SourceRef(table="prescriptions", primary_key=1, label="Rx"),
        name="Metformin",
        dose="1000 mg",
        status="active",
    )
    h = ev.add(med.HANDLE_PREFIX, med.source, med.evidence_text())
    res = verify(
        [RawClaim(text="Patient takes 3 active medications including Metformin", handles=[h])],
        ev, [], [], [],
    )
    assert len(res.verified_claims) == 1


def test_year_bare_integer_is_not_a_value():
    """PROTECTED FALSE-POSITIVE: a 4-digit year ('2019') is not a lab value and
    need not appear in cited evidence."""
    ev = EvidenceSet()
    med = Medication(
        source=SourceRef(table="prescriptions", primary_key=2, label="Rx"),
        name="Metformin",
        dose="1000 mg",
        status="active",
    )
    h = ev.add(med.HANDLE_PREFIX, med.source, med.evidence_text())
    res = verify(
        [RawClaim(text="Metformin has been active since 2019", handles=[h])],
        ev, [], [], [],
    )
    assert len(res.verified_claims) == 1


# --- Finding 3: guideline text cannot ground a patient value -------------------


def _a1c_plus_guideline(gl_quote: str):
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
    chunk = _gl_chunk(gl_quote)
    gh = ev.add(chunk.HANDLE_PREFIX, chunk.ref, chunk.evidence_text())
    return ev, lh, gh


def test_guideline_number_cannot_ground_patient_value():
    """A fabricated patient A1c of 7.0 that appears ONLY in the cited guideline
    target must be quarantined — even though a real patient handle is co-cited."""
    ev, lh, gh = _a1c_plus_guideline(
        "ADA guidance: the hemoglobin A1c target is 7.0 % or below for most adults."
    )
    res = verify([RawClaim(text="Her hemoglobin A1c is 7.0", handles=[lh, gh])], ev, [], [], [])
    assert res.verified_claims == []
    assert res.guideline_evidence == []
    assert len(res.quarantined_claims) == 1


def test_legit_mixed_claim_still_verifies():
    """The legit mixed case is preserved: the patient value (9.2) grounds on the
    patient row and the guideline target is reference context — verified, in the
    briefing, carrying both refs."""
    ev, lh, gh = _a1c_plus_guideline(
        "Our practice targets hemoglobin A1c below 7 % for most nonpregnant adults."
    )
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
