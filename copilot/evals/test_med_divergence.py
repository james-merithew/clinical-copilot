"""DB-integration regression eval for the medication divergence surface
(app.tools.medications.get_medications) against the live seed.

OpenEMR records meds in both `prescriptions` and `lists`, which can diverge and
where `active` can be stale (AUDIT.md). pid 101's Metformin is ACTIVE in
prescriptions but INACTIVE in lists. The tool must surface that divergence and
attribute the med to the AUTHORITATIVE `prescriptions` row — never silently
smooth the conflict.
"""

from __future__ import annotations

from app.tools.medications import get_medications


def _find(meds, name):
    for m in meds:
        if m.name.lower() == name.lower():
            return m
    return None


def test_metformin_divergence_attributed_to_prescriptions_row(make_ctx, q):
    """REGRESSION (med divergence + attribution): pid 101's Metformin must come
    back with diverges=True and a source bound to the real `prescriptions` PK —
    guards against the tool dropping the divergence flag or attributing to the
    wrong table (a silent-reconciliation regression). The expected PK is
    discovered at runtime, not hardcoded."""
    # Runtime discovery of the authoritative prescriptions row PK.
    rx = q(
        "SELECT id FROM prescriptions WHERE patient_id = 101 AND LOWER(drug) = 'metformin' LIMIT 1"
    )
    assert rx, "seed precondition: Metformin prescription for pid 101"
    expected_pk = int(rx[0]["id"])

    meds = get_medications(make_ctx(101)).medications
    metformin = _find(meds, "Metformin")
    assert metformin is not None, "Metformin should be surfaced"
    assert metformin.diverges is True
    assert metformin.divergence_note  # human-readable explanation present
    assert metformin.source.table == "prescriptions"
    assert metformin.source.primary_key == expected_pk


def test_nondiverging_med_not_flagged(make_ctx):
    """REGRESSION (false-positive guard): pid 101's Lisinopril has no conflicting
    list row, so it must NOT be flagged as diverging — guards against the
    divergence detector firing on every med."""
    meds = get_medications(make_ctx(101)).medications
    lisinopril = _find(meds, "Lisinopril")
    assert lisinopril is not None
    assert lisinopril.diverges is False
