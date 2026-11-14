"""Integration (live local DB): demographics + snapshot + authz.

Guards: (security) snapshot must deny the not-your-provider fixture; (regression)
the snapshot must expose demographics, a lab trend series, and the rising-trend
flag for the showcase patient.
"""

from app.authz import authorize
from app.main import _build_snapshot
from app.tools import ToolContext
from app.tools.demographics import get_demographics


def test_demographics_showcase_a():
    d = get_demographics(ToolContext("t", 1, 101))
    assert "Delgado" in d.name
    assert isinstance(d.age, int) and d.age > 0
    assert d.sex
    assert d.source.table == "patient_data"


def test_snapshot_shape_and_trend():
    s = _build_snapshot(ToolContext("t", 1, 101))
    assert s.demographics and "Delgado" in s.demographics.name
    assert any("a1c" in k.lower() for k in s.labs_by_test), "expected an A1c series"
    assert any("Rising trend" in f.message for f in s.constraint_flags)
    assert not s.degraded


def test_snapshot_denies_other_provider_patient():
    # pid 23 is assigned to a different provider → user 1 is not authorized.
    assert authorize(1, 23).allowed is False
