"""HIPAA invariant (hermetic): the logging layer cannot emit PHI.

Failure mode guarded: a developer (or an exception path) passes a PHI value to
`log_event`; without enforcement it would be serialized to the log sink. The
allowlist must drop it.
"""

from app.correlation import _ALLOWED_LOG_KEYS, _sanitize


def test_disallowed_keys_dropped():
    out = _sanitize({"pid": 101, "name": "Marcus Delgado", "drug": "Amoxicillin 500 mg"})
    assert out["pid"] == 101
    assert "name" not in out
    assert "drug" not in out
    assert out["_dropped_fields"] == 2


def test_allowlisted_scalars_pass():
    out = _sanitize({"user_id": 1, "reason": "not_patient_provider", "latency_ms": 12.3})
    assert out == {"user_id": 1, "reason": "not_patient_provider", "latency_ms": 12.3}


def test_allowlist_contains_no_phi_shaped_keys():
    # The allowlist is ids/counts/timings/flags only — never name/drug/text/etc.
    for phi_key in ("name", "fname", "lname", "drug", "text", "title", "reason_text", "note"):
        assert phi_key not in _ALLOWED_LOG_KEYS
