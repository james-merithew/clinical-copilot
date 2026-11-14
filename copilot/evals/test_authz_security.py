"""DB-integration security evals for the Authorization Gateway (app.authz).

The two-layer user<->patient check (CLAUDE.md #2) is the trust boundary: it must
ALLOW the assigned provider, DENY a patient owned by another provider, and DENY a
non-existent patient — all fail-closed, against the live seeded DB.
"""

from __future__ import annotations

from app.authz import authorize

DEMO_USER = 1


def test_assigned_provider_is_allowed(q):
    """SECURITY (positive path): the demo physician IS pid 101's assigned
    provider, so access is granted — guards against an over-strict gateway that
    would lock the real provider out. Provider assignment is read at runtime."""
    row = q("SELECT providerID FROM patient_data WHERE pid = 101")
    assert row and int(row[0]["providerID"]) == DEMO_USER, "seed precondition: pid 101 -> user 1"
    decision = authorize(DEMO_USER, 101)
    assert decision.allowed is True
    assert decision.reason == "authorized"


def test_other_providers_patient_is_denied(q):
    """SECURITY (per-patient scope): pid 23 is assigned to a DIFFERENT provider,
    so the demo user must be denied with 'not_patient_provider' — guards the
    per-patient scoping layer gacl does not enforce (invariant #2)."""
    row = q("SELECT providerID FROM patient_data WHERE pid = 23")
    assert row and int(row[0]["providerID"]) != DEMO_USER, "seed precondition: pid 23 -> other provider"
    decision = authorize(DEMO_USER, 23)
    assert decision.allowed is False
    assert decision.reason == "not_patient_provider"


def test_missing_patient_is_denied(q):
    """SECURITY (fail-closed on missing data): an unknown pid must be denied with
    'patient_not_found', never allowed by default — guards against a permissive
    fallback when the patient row is absent."""
    assert not q("SELECT pid FROM patient_data WHERE pid = 999"), "seed precondition: pid 999 absent"
    decision = authorize(DEMO_USER, 999)
    assert decision.allowed is False
    assert decision.reason == "patient_not_found"
