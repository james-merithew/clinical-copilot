"""DB-integration eval for the constraint engine over LIVE tool output.

pid 102 (Eleanor Whitfield) is the verifier money-shot: a documented Penicillin
allergy alongside active Amoxicillin (allergy<->med, same class) AND active
Warfarin + Ibuprofen (high-risk drug-drug). Running the real tools then the real
verifier must yield BOTH flags — proving the constraint scan fires on data
pulled from the DB, not just hand-built fixtures.
"""

from __future__ import annotations

from app.evidence import EvidenceSet
from app.tools.labs import get_labs
from app.tools.medications import get_medications
from app.tools.problems import get_problems_allergies
from app.verifier import verify


def test_pid102_yields_allergy_and_drug_drug_flags(make_ctx):
    """CONSTRAINT (integration): live meds/allergies for pid 102 must produce a
    critical allergy_interaction AND a drug_drug warning — guards the end-to-end
    path (tools -> structured records -> deterministic constraint scan). Uses no
    model prose, so the flags cannot be argued away."""
    ctx = make_ctx(102)
    meds = get_medications(ctx).medications
    pa = get_problems_allergies(ctx)
    labs = get_labs(ctx).labs

    res = verify([], EvidenceSet(), meds, pa.allergies, labs)
    kinds = {f.kind for f in res.constraint_flags}

    allergy_flags = [f for f in res.constraint_flags if f.kind == "allergy_interaction"]
    drug_flags = [f for f in res.constraint_flags if f.kind == "drug_drug"]

    assert "allergy_interaction" in kinds, "Penicillin allergy vs active Amoxicillin"
    assert "drug_drug" in kinds, "Warfarin + Ibuprofen bleeding risk"
    assert any(f.severity.value == "critical" for f in allergy_flags)
    assert any(f.severity.value == "warning" for f in drug_flags)
