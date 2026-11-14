"""`GET /labs/trend` — deterministic lab-trend series for the chart widget (T5).

A sibling of `/snapshot`: no LLM, same session-token + per-patient authorization
gate, every point is a `LabResult` carrying its real `procedure_result` PK.
Extracted-PDF lab values persist as `procedure_result` rows, so document-derived
Observations flow into the trend automatically through `get_labs`.

Failure modes guarded:
- endpoint reachable without a session token          -> auth bypass;
- token for patient A used to trend patient B         -> cross-patient access;
- series unsorted / keyed wrong                        -> chart renders garbage
  (out-of-order polyline) and the physician misreads a trend;
- a point without its procedure_result PK              -> citation invariant
  broken (chart shows an unattributable value as fact);
- labs tool failure turning into a 500 or invented data -> must degrade to an
  explicit empty-series + notes response (never fabricate, never crash).
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main, session_token
from app.authz import AuthzDecision
from app.contracts import LabResult, LabsResult, LabTrendResponse, SourceRef
from app.main import app
from app.session_token import mint

TEST_KEY = "unit-test-signing-key-000000000000000"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(session_token, "get_signing_key", lambda: TEST_KEY)
    with TestClient(app) as c:
        yield c, monkeypatch


def _tok(pid, scope="patient", sub=1, now=None, ttl=900):
    return mint(sub, "tester", pid, scope, "patients:demo", TEST_KEY, ttl, now or int(time.time()))


def _lab(pk, name, value, date, abnormal=False, units=None, rng=None):
    return LabResult(
        source=SourceRef(table="procedure_result", primary_key=pk, label=f"{name} {date}"),
        name=name,
        value=value,
        units=units,
        range=rng,
        abnormal=abnormal,
        date=date,
    )


# Deliberately shuffled: dates out of order, two tests interleaved.
_STUB_LABS = [
    _lab(9003, "Hemoglobin A1c", "8.2", "2026-01-10", abnormal=True, units="%", rng="4.0-5.6"),
    _lab(9001, "Glucose", "105", "2025-11-02", units="mg/dL"),
    _lab(9002, "Hemoglobin A1c", "7.1", "2025-07-14", abnormal=True, units="%", rng="4.0-5.6"),
    _lab(9004, "Glucose", "99", "2025-05-30", units="mg/dL"),
    _lab(9005, "Hemoglobin A1c", "6.4", "2024-12-01", units="%", rng="4.0-5.6"),
]


def _allow(mp):
    mp.setattr(main, "authorize", lambda u, p: AuthzDecision(True, u, p, "authorized"))


def _stub_labs(mp, labs=None, truncated=False, expect_name=None, raises=False):
    def fake_get_labs(ctx, name=None):  # noqa: ANN001
        if raises:
            raise RuntimeError("db down")
        if expect_name is not None:
            assert name == expect_name
        return LabsResult(labs=list(labs or []), truncated=truncated)

    mp.setattr(main, "get_labs", fake_get_labs)


def _get(c, pid=101, token_pid=101, query="", headers=True):
    h = {"Authorization": f"Bearer {_tok(token_pid)}"} if headers else {}
    return c.get(f"/labs/trend?pid={pid}{query}", headers=h)


# ---- auth edge (both layers must hold on this new read path) ----------------


def test_trend_without_token_is_401(client):
    c, _ = client
    assert _get(c, headers=False).status_code == 401


def test_token_for_patient_a_cannot_trend_patient_b(client):
    """A pid-101 token must be refused on ?pid=102 — token binding, not authorize."""
    c, mp = client
    _allow(mp)
    _stub_labs(mp, _STUB_LABS)
    r = _get(c, pid=102, token_pid=101)
    assert r.status_code == 403
    body = r.json()
    assert body["reason"] == "not_authorized"
    assert "token_patient_mismatch" in body["detail"]


# ---- series shape ------------------------------------------------------------


def test_series_grouped_by_test_and_dates_ascending(client):
    c, mp = client
    _allow(mp)
    _stub_labs(mp, _STUB_LABS)
    r = _get(c)
    assert r.status_code == 200
    data = r.json()
    assert set(data["series"]) == {"Hemoglobin A1c", "Glucose"}
    a1c = data["series"]["Hemoglobin A1c"]
    dates = [p["date"] for p in a1c]
    assert dates == sorted(dates) == ["2024-12-01", "2025-07-14", "2026-01-10"]
    glu = data["series"]["Glucose"]
    assert [p["date"] for p in glu] == ["2025-05-30", "2025-11-02"]
    # LabResult fields survive intact (the chart needs value/units/abnormal).
    latest = a1c[-1]
    assert latest["value"] == "8.2"
    assert latest["units"] == "%"
    assert latest["range"] == "4.0-5.6"
    assert latest["abnormal"] is True


def test_every_point_cites_its_procedure_result_pk(client):
    """Citation invariant: each chart point binds to the real row the DB returned."""
    c, mp = client
    _allow(mp)
    _stub_labs(mp, _STUB_LABS)
    data = _get(c).json()
    stub_pks = {lab.source.primary_key for lab in _STUB_LABS}
    seen = set()
    for pts in data["series"].values():
        for p in pts:
            assert p["source"]["table"] == "procedure_result"
            seen.add(p["source"]["primary_key"])
    assert seen == stub_pks


def test_test_filter_passes_through_and_echoes(client):
    """?test= reaches get_labs as `name` (SQL-side filter) and echoes back."""
    c, mp = client
    _allow(mp)
    _stub_labs(mp, [x for x in _STUB_LABS if "A1c" in x.name], expect_name="A1c")
    data = _get(c, query="&test=A1c").json()
    assert data["test"] == "A1c"
    assert set(data["series"]) == {"Hemoglobin A1c"}


def test_truncated_flag_propagates(client):
    c, mp = client
    _allow(mp)
    _stub_labs(mp, _STUB_LABS, truncated=True)
    assert _get(c).json()["truncated"] is True


def test_labs_failure_degrades_to_empty_series_not_500(client):
    """Graceful degradation (invariant 9): the tool blowing up must yield an
    explicit gap — 200, empty series, a note — never a 500, never invented points."""
    c, mp = client
    _allow(mp)
    _stub_labs(mp, raises=True)
    r = _get(c)
    assert r.status_code == 200
    data = r.json()
    assert data["series"] == {}
    assert data["notes"] == "labs unavailable"
    assert data["correlation_id"]


# ---- pure helper -------------------------------------------------------------


def test_build_lab_trend_groups_and_sorts(monkeypatch):
    monkeypatch.setattr(main, "get_labs",
                        lambda ctx, name=None: LabsResult(labs=list(_STUB_LABS), truncated=False))
    out = main._build_lab_trend(object(), None)
    assert isinstance(out, LabTrendResponse)
    assert set(out.series) == {"Hemoglobin A1c", "Glucose"}
    for pts in out.series.values():
        d = [p.date for p in pts]
        assert d == sorted(d)
    assert out.truncated is False and out.notes is None


def test_build_lab_trend_none_dates_sort_stably(monkeypatch):
    """Boundary: rows with a NULL date (expected in real data) must not crash the
    sort — they sort first (empty key), everything else stays chronological."""
    labs = [_lab(1, "TSH", "2.1", "2025-03-01"), _lab(2, "TSH", "1.9", None)]
    monkeypatch.setattr(main, "get_labs",
                        lambda ctx, name=None: LabsResult(labs=labs, truncated=False))
    out = main._build_lab_trend(object(), "TSH")
    assert [p.source.primary_key for p in out.series["TSH"]] == [2, 1]
    assert out.test == "TSH"
