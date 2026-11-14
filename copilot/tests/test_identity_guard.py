"""Current-patient identity guard — a name echoed from un-scoped history must never be
presented as this chart's patient (QA defect #1).

Failure mode guarded: a prior cross-patient turn left another patient's name in the
client-supplied history; the answerer (whose prompt never carried the current patient's
name) echoed "the patient in this chart is <other name>" into the quarantined prose.
No data leak (tools stay on ctx.pid), but surfacing the wrong patient's identity is a
real correctness/safety issue. The deterministic guard in _finalize drops any prose that
asserts a DIFFERENT patient, disclosed in notes; the anchor gives the model ground truth.
"""

from __future__ import annotations

from app.correlation import set_correlation_id
from app.graph.workers import (
    _asserts_foreign_patient,
    _current_patient_name,
    _identity_anchor,
    _finalize,
    _Turn,
)
from app.observability import TurnObserver

FOREIGN = "The patient in this chart is Eleanor Whitfield (she/her)."


# ---- detector -----------------------------------------------------------------


def test_detector_flags_a_foreign_identity_assertion():
    assert _asserts_foreign_patient(FOREIGN, "Marcus Delgado") is True
    assert _asserts_foreign_patient("this chart is Eleanor Whitfield", "Marcus Delgado") is True


def test_detector_does_not_flag_the_current_patient():
    assert _asserts_foreign_patient("The patient is Marcus Delgado.", "Marcus Delgado") is False
    # partial name of the current patient is fine (shares a token)
    assert _asserts_foreign_patient("Delgado is due for an A1c.", "Marcus Delgado") is False


def test_detector_no_false_positive_on_clinical_text():
    for t in [
        "Metformin 1000 mg BID (prescriptions#79).",
        "eGFR 58 mL/min (procedure_result#60).",
        "Per ACC/AHA 2017, the BP target is <130/80.",
        "HbA1c 8.6% flagged abnormal.",
    ]:
        assert _asserts_foreign_patient(t, "Marcus Delgado") is False


def test_anchor_names_the_patient_or_is_empty():
    assert "Marcus Delgado" in _identity_anchor("Marcus Delgado")
    assert _identity_anchor(None) == ""


def test_current_patient_name_is_none_without_a_db():
    # hermetic: no DB -> get_demographics raises -> None (anchor/guard become no-ops)
    class _Ctx:
        pid = 101

    assert _current_patient_name(_Ctx()) is None


# ---- guard end-to-end (via _finalize) -----------------------------------------


def _fin(brief, current_name):
    set_correlation_id("identity-guard")
    return _finalize(brief, _Turn(), TurnObserver(101, "message"), current_name=current_name)


def test_guard_strips_foreign_name_from_all_channels():
    # an uncited foreign-name claim (-> quarantine) plus foreign notes
    brief = {"claims": [{"text": FOREIGN, "handles": []}], "notes": FOREIGN}
    resp = _fin(brief, "Marcus Delgado")
    blob = " ".join(
        [c.text for c in resp.briefing]
        + [c.text for c in resp.guideline_evidence]
        + [c.text for c in resp.quarantined_claims]
        + [resp.notes or ""]
    )
    assert "Eleanor Whitfield" not in blob          # the wrong name is gone everywhere
    assert "named a different patient" in (resp.notes or "")  # disclosed, never silent


def test_without_a_current_name_the_guard_is_a_noop():
    # control: no resolved name -> the foreign claim survives (in quarantine).
    brief = {"claims": [{"text": FOREIGN, "handles": []}]}
    resp = _fin(brief, None)
    assert any("Eleanor Whitfield" in c.text for c in resp.quarantined_claims)
