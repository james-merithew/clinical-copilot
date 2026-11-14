"""Hermetic tests for the broadened alert engine (levels, trends, new DDIs).

Failure modes guarded: (invariant) a value outside its reference range must flag
even when the source 'abnormal' bit is unset; (regression) a worsening lab trend
must be surfaced; a flat trend must NOT be; a new high-risk pair must fire.
"""

from app.contracts import Allergy, LabResult, Medication, SourceRef
from app.verifier import _check_constraints, _out_of_range


def _lab(pk, name, value, rng, date, abn=False):
    return LabResult(
        source=SourceRef(table="procedure_result", primary_key=pk, label=name),
        name=name, value=value, units="", range=rng, abnormal=abn, date=date,
    )


def _med(pk, name):
    return Medication(
        source=SourceRef(table="prescriptions", primary_key=pk, label=name),
        name=name, status="active",
    )


def test_out_of_range_parsing():
    assert _out_of_range("8.2", "4.0-5.6") is True
    assert _out_of_range("5.0", "4.0-5.6") is False
    assert _out_of_range("160", "<150") is True
    assert _out_of_range("30", ">40") is True
    assert _out_of_range("", "4-5") is False


def test_level_flag_from_range_without_source_abnormal():
    lab = _lab(1, "Glucose", "180", "70-99", "2026-01-01", abn=False)
    flags = _check_constraints([], [], [lab])
    assert any(f.kind == "threshold" and "Glucose" in f.message for f in flags)


def test_rising_trend_flag():
    labs = [
        _lab(1, "Hemoglobin A1c", "6.9", "4.0-5.6", "2025-05-01", abn=True),
        _lab(2, "Hemoglobin A1c", "7.5", "4.0-5.6", "2025-11-01", abn=True),
        _lab(3, "Hemoglobin A1c", "8.2", "4.0-5.6", "2026-05-01", abn=True),
    ]
    flags = _check_constraints([], [], labs)
    assert any("Rising trend" in f.message for f in flags)


def test_no_trend_when_flat():
    labs = [
        _lab(1, "Hemoglobin A1c", "6.0", "4.0-5.6", "2025-05-01", abn=True),
        _lab(2, "Hemoglobin A1c", "6.0", "4.0-5.6", "2025-11-01", abn=True),
        _lab(3, "Hemoglobin A1c", "6.0", "4.0-5.6", "2026-05-01", abn=True),
    ]
    flags = _check_constraints([], [], labs)
    assert not any("Rising trend" in f.message for f in flags)


def test_abnormal_labs_dedup_to_latest():
    # 3 abnormal A1c results → only the most recent is flagged as abnormal (not 3).
    labs = [
        _lab(1, "Hemoglobin A1c", "8.2", "4.0-5.6", "2026-05-01", abn=True),
        _lab(2, "Hemoglobin A1c", "7.5", "4.0-5.6", "2025-11-01", abn=True),
    ]
    abn = [f for f in _check_constraints([], [], labs) if f.message.startswith("Abnormal lab")]
    assert len(abn) == 1 and "8.2" in abn[0].message


def test_opioid_benzo_ddi_fires():
    flags = _check_constraints([_med(1, "Oxycodone"), _med(2, "Lorazepam")], [], [])
    assert any(f.kind == "drug_drug" and "respiratory" in f.message.lower() for f in flags)
