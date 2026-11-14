"""Hermetic tests for the metric-card builder + abnormal-vital flags.

Guards: (invariant) status/color is computed deterministically from thresholds;
(regression) BP high / A1c high / eGFR stable map correctly; empty vitals emit no
metrics (graceful degradation); an abnormal vital raises a notification flag.
"""

from app.contracts import LabResult, SourceRef, Vital
from app.metrics import build_lab_metrics, build_vital_metrics
from app.verifier import _check_constraints


def _vit(bps, bpd, pulse, bmi, date):
    return Vital(
        source=SourceRef(table="form_vitals", primary_key=1, label="v"),
        date=date, bps=bps, bpd=bpd, pulse=pulse, bmi=bmi,
    )


def _lab(name, value, date):
    return LabResult(
        source=SourceRef(table="procedure_result", primary_key=1, label=name),
        name=name, value=value, units="", range="", abnormal=False, date=date,
    )


def test_vital_metrics_status():
    m = {x.key: x for x in build_vital_metrics([_vit(155, 99, 88, 29.4, "2026-06-14")])}
    assert m["bp"].value == "155/99" and m["bp"].status == "high"
    assert m["hr"].status == "normal"
    assert m["bmi"].status == "elevated" and m["bmi"].status_label == "Overweight"


def test_lab_metrics_status():
    m = {x.key: x for x in build_lab_metrics([
        _lab("Hemoglobin A1c", "8.2", "2026-05-01"),
        _lab("LDL Cholesterol", "165", "2026-05-01"),
        _lab("eGFR", "61", "2026-05-01"),
    ])}
    assert m["a1c"].status == "high"
    assert m["ldl"].status == "high"
    assert m["egfr"].status == "normal" and m["egfr"].status_label == "Stable"


def test_metric_uses_latest_and_keeps_history():
    m = build_lab_metrics([
        _lab("Hemoglobin A1c", "6.9", "2025-05-01"),
        _lab("Hemoglobin A1c", "8.2", "2026-05-01"),
    ])
    a1c = m[0]
    assert a1c.value == "8.2" and len(a1c.history) == 2


def test_empty_vitals_yields_no_metrics():
    assert build_vital_metrics([]) == []


def test_abnormal_bp_raises_flag():
    flags = _check_constraints([], [], [], vitals=[_vit(155, 99, 88, 29.4, "2026-06-14")])
    assert any("blood pressure" in f.message.lower() for f in flags)
