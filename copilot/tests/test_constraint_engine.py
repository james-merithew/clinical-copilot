"""Hermetic unit tests for the deterministic constraint engine
(app.verifier._check_constraints via the public verify()).

Constraint enforcement (CLAUDE.md #4, W1_ARCHITECTURE.md §9) runs over STRUCTURED
evidence, not the model's prose, so it cannot be talked out of firing. These
tests pin the two clinical rules that matter for the showcase and — critically —
that two unrelated meds produce NO flag (false-positive guard).

No DB, no LLM: meds/allergies are built in-code with fake SourceRefs.
"""

from __future__ import annotations

from app.contracts import Allergy, Medication, SourceRef
from app.evidence import EvidenceSet
from app.verifier import verify


def _med(name: str, pk: int, dose: str | None = None) -> Medication:
    return Medication(
        source=SourceRef(table="prescriptions", primary_key=pk, label=f"Rx {name}"),
        name=name,
        dose=dose,
        status="active",
    )


def _allergy(substance: str, pk: int) -> Allergy:
    return Allergy(
        source=SourceRef(table="lists", primary_key=pk, label=f"Allergy: {substance}"),
        substance=substance,
    )


def test_penicillin_allergy_plus_amoxicillin_flags_critical():
    """CONSTRAINT (allergy_interaction): a penicillin-class allergy against an
    active amoxicillin (same class) MUST raise a critical flag — guards the
    class-aware cross-reactivity rule; a naive string match would miss it."""
    meds = [_med("Amoxicillin", 10, "500 mg")]
    algs = [_allergy("Penicillin", 20)]
    res = verify([], EvidenceSet(), meds, algs, [])
    hits = [f for f in res.constraint_flags if f.kind == "allergy_interaction"]
    assert len(hits) == 1
    assert hits[0].severity.value == "critical"
    # Flag binds both the allergy row and the med row.
    tables = {r.table for r in hits[0].source_refs}
    assert tables == {"lists", "prescriptions"}


def test_warfarin_plus_ibuprofen_flags_drug_drug():
    """CONSTRAINT (drug_drug): warfarin + an NSAID (ibuprofen) MUST raise a
    drug_drug warning — guards the high-risk bleeding-interaction pair via the
    NSAID class expansion."""
    meds = [_med("Warfarin", 30, "5 mg"), _med("Ibuprofen", 31, "400 mg")]
    res = verify([], EvidenceSet(), meds, [], [])
    hits = [f for f in res.constraint_flags if f.kind == "drug_drug"]
    assert len(hits) == 1
    assert hits[0].severity.value == "warning"


def test_two_unrelated_meds_produce_no_flags():
    """CONSTRAINT (false-positive guard): two unrelated meds with no allergy and
    no known interaction MUST produce ZERO flags — guards against an over-eager
    engine that cries wolf and trains physicians to ignore alerts."""
    meds = [_med("Metformin", 40, "1000 mg"), _med("Lisinopril", 41, "10 mg")]
    res = verify([], EvidenceSet(), meds, [], [])
    assert res.constraint_flags == []
