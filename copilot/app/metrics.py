"""Deterministic metric cards — vitals + key labs → value + status + trend.

Status/color is computed from a small clinical threshold table (NOT the LLM).
Each Metric carries a SourceRef so the card is source-attributed. Only metrics
that actually have data are emitted (graceful degradation).
"""

from __future__ import annotations

from .contracts import LabResult, Metric, MetricPoint, Vital


def _num(s) -> float | None:  # noqa: ANN001
    try:
        return float(str(s).strip().split()[0])
    except Exception:  # noqa: BLE001
        return None


# ---- vital status thresholds ----
def _bp_status(sys, dia):  # noqa: ANN001
    if sys is None:
        return ("normal", "")
    if sys >= 140 or (dia and dia >= 90):
        return ("high", "High")
    if sys >= 120:
        return ("elevated", "Elevated")
    return ("normal", "Normal")


def _hr_status(p):  # noqa: ANN001
    if p is None:
        return ("normal", "")
    if p > 100:
        return ("high", "High")
    if p < 60:
        return ("low", "Low")
    return ("normal", "Normal")


def _bmi_status(b):  # noqa: ANN001
    if b is None:
        return ("normal", "")
    if b >= 30:
        return ("high", "Obese")
    if b >= 25:
        return ("elevated", "Overweight")
    if b < 18.5:
        return ("low", "Underweight")
    return ("normal", "Normal")


def build_vital_metrics(vitals: list[Vital]) -> list[Metric]:
    out: list[Metric] = []
    if not vitals:
        return out
    latest = vitals[0]
    asc = list(reversed(vitals))  # chronological for the sparkline

    if latest.bps and latest.bpd:
        st, lab = _bp_status(latest.bps, latest.bpd)
        hist = [MetricPoint(value=f"{int(v.bps)}/{int(v.bpd)}", date=v.date) for v in asc if v.bps and v.bpd]
        out.append(Metric(key="bp", label="Blood Pressure", value=f"{int(latest.bps)}/{int(latest.bpd)}",
                          unit="mmHg", status=st, status_label=lab, group="vital", history=hist, source=latest.source))
    if latest.pulse:
        st, lab = _hr_status(latest.pulse)
        hist = [MetricPoint(value=str(int(v.pulse)), date=v.date) for v in asc if v.pulse]
        out.append(Metric(key="hr", label="Heart Rate", value=str(int(latest.pulse)), unit="bpm",
                          status=st, status_label=lab, group="vital", history=hist, source=latest.source))
    if latest.bmi:
        st, lab = _bmi_status(latest.bmi)
        hist = [MetricPoint(value=f"{v.bmi:g}", date=v.date) for v in asc if v.bmi]
        out.append(Metric(key="bmi", label="BMI", value=f"{latest.bmi:g}", unit="",
                          status=st, status_label=lab, group="vital", history=hist, source=latest.source))
    return out


# ---- lab status thresholds ----
def _a1c_status(v):  # noqa: ANN001
    return ("high", "High") if v >= 6.5 else ("elevated", "Elevated") if v >= 5.7 else ("normal", "Normal")


def _egfr_status(v):  # noqa: ANN001
    return ("normal", "Stable") if v >= 60 else ("elevated", "Reduced") if v >= 30 else ("high", "Low")


def _ldl_status(v):  # noqa: ANN001
    return ("high", "High") if v >= 160 else ("elevated", "Borderline") if v >= 100 else ("normal", "Optimal")


def _creat_status(v):  # noqa: ANN001
    return ("high", "High") if v > 1.3 else ("low", "Low") if v < 0.6 else ("normal", "Normal")


# key -> (matchers, default unit, status_fn, label)
LAB_METRICS = [
    ("a1c", ("a1c", "hemoglobin a1c"), "%", _a1c_status, "HbA1c"),
    ("egfr", ("egfr", "glomerular filtration"), "mL/min", _egfr_status, "eGFR"),
    ("ldl", ("ldl",), "mg/dL", _ldl_status, "LDL"),
    ("creat", ("creatinine",), "mg/dL", _creat_status, "Creatinine"),
]


def build_lab_metrics(labs: list[LabResult]) -> list[Metric]:
    out: list[Metric] = []
    for key, matchers, unit, status_fn, label in LAB_METRICS:
        series = [lab for lab in labs if any(m in lab.name.lower() for m in matchers)]
        if not series:
            continue
        series.sort(key=lambda x: x.date or "")
        latest = series[-1]
        v = _num(latest.value)
        if v is None:
            continue
        st, slab = status_fn(v)
        hist = [MetricPoint(value=str(x.value), date=x.date) for x in series if _num(x.value) is not None]
        out.append(Metric(key=key, label=label, value=str(latest.value), unit=(latest.units or unit),
                          status=st, status_label=slab, group="lab", history=hist, source=latest.source))
    return out


def build_metrics(vitals: list[Vital], labs: list[LabResult]) -> list[Metric]:
    return build_vital_metrics(vitals) + build_lab_metrics(labs)
