"""Session-token auth — the Week-2 security fix (AUTH_TOKEN_SPEC.md).

Replaces the reusable static bearer (exposed in the browser) with a short-lived,
OpenEMR-minted, patient-bound token. These tests are the denial/cross-user/
tamper coverage the grader asked for, at both the crypto layer and the HTTP edge.

Failure modes guarded:
- forged / tampered / expired token accepted  -> auth bypass;
- a token for patient A used on patient B      -> cross-patient data access;
- a day-scope token used on a patient endpoint -> scope confusion;
- a valid token whose user is NOT the patient's provider -> cross-user access
  (the per-patient scope layer on top of the OpenEMR ACL);
- verification depending on JSON re-serialization -> PHP<->Python drift.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main, session_token
from app.authz import AuthzDecision
from app.main import app
from app.session_token import TokenInvalid, mint, verify

# The spec's golden interop vector — PHP mints, Python verifies; both pin this.
GOLDEN_KEY = "golden-test-key-do-not-use-in-prod"
GOLDEN_TOKEN = (
    "eyJhY2wiOiJwYXRpZW50czpkZW1vIiwiZXhwIjoxNzUyMDAwOTAwLCJpYXQiOjE3NTIwMDAwMDAsInBpZCI6MTAxL"
    "CJzY29wZSI6InBhdGllbnQiLCJzdWIiOjQyLCJ1c3IiOiJkcmNoZW4iLCJ2IjoxfQ."
    "WCeKuHjl7PtagFHKyHJjQmxn8EigC9nvjytqSFl_h8A"
)
GOLDEN_NOW = 1752000100  # between iat and exp

TEST_KEY = "unit-test-signing-key-000000000000000"


# ---- crypto layer ----------------------------------------------------------------


def test_golden_vector_verifies():
    """The exact token an independent minter (PHP) produces must verify here."""
    c = verify(GOLDEN_TOKEN, GOLDEN_KEY, now=GOLDEN_NOW)
    assert (c.user_id, c.usr, c.pid, c.scope, c.acl) == (42, "drchen", 101, "patient", "patients:demo")


@pytest.mark.parametrize("bad", [GOLDEN_TOKEN[:-1] + "_", GOLDEN_TOKEN[:-1], "no-dot", "a.b.c", ""])
def test_tampered_or_malformed_rejected(bad):
    with pytest.raises(TokenInvalid):
        verify(bad, GOLDEN_KEY, now=GOLDEN_NOW)


def test_wrong_key_rejected():
    with pytest.raises(TokenInvalid):
        verify(GOLDEN_TOKEN, "not-the-key", now=GOLDEN_NOW)


def test_expired_rejected():
    with pytest.raises(TokenInvalid):
        verify(GOLDEN_TOKEN, GOLDEN_KEY, now=1752000900 + 1000)  # well past exp + skew


def test_verify_does_not_reserialize_json():
    """Verification must work over the LITERAL payload segment, so a payload whose
    JSON key order is NOT canonical still verifies (no PHP<->Python drift)."""
    import base64
    import hashlib
    import hmac
    import json

    def b64u(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    # deliberately unsorted keys + spaces — a serialization Python's mint never makes
    raw = json.dumps({"v": 1, "sub": 7, "usr": "x", "pid": 5, "scope": "patient",
                      "acl": "patients:demo", "iat": GOLDEN_NOW, "exp": GOLDEN_NOW + 900},
                     separators=(", ", ": "))
    pb = b64u(raw.encode())
    sig = b64u(hmac.new(TEST_KEY.encode(), pb.encode("ascii"), hashlib.sha256).digest())
    c = verify(pb + "." + sig, TEST_KEY, now=GOLDEN_NOW)
    assert c.user_id == 7 and c.pid == 5


def test_mint_verify_roundtrip():
    now = int(time.time())
    t = mint(9, "u", 101, "patient", "patients:demo", TEST_KEY, 900, now)
    assert verify(t, TEST_KEY, now=now + 10).user_id == 9


# ---- HTTP edge (all short-circuit before the agent runs) -------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(session_token, "get_signing_key", lambda: TEST_KEY)
    with TestClient(app) as c:
        yield c, monkeypatch


def _tok(pid, scope="patient", sub=1, now=None, ttl=900):
    return mint(sub, "tester", pid, scope, "patients:demo", TEST_KEY, ttl, now or int(time.time()))


def _chat(client, headers=None, pid=101):
    return client.post("/chat", json={"pid": pid, "message": "hi"}, headers=headers or {})


def test_chat_without_token_is_401(client):
    c, _ = client
    assert _chat(c).status_code == 401


def test_chat_expired_token_is_401(client):
    c, _ = client
    old = int(time.time()) - 100_000
    assert _chat(c, {"Authorization": f"Bearer {_tok(101, now=old)}"}).status_code == 401


def test_chat_tampered_token_is_401(client):
    c, _ = client
    bad = _tok(101)[:-1] + ("A" if _tok(101)[-1] != "A" else "B")
    assert _chat(c, {"Authorization": f"Bearer {bad}"}).status_code == 401


def test_token_for_patient_a_cannot_act_on_patient_b(client):
    """The core binding: a token minted for pid 101 must be refused on pid 202."""
    c, mp = client
    mp.setattr(main, "authorize", lambda u, p: AuthzDecision(True, u, p, "authorized"))
    r = _chat(c, {"Authorization": f"Bearer {_tok(101)}"}, pid=202)  # token pid != body pid
    assert r.status_code == 403
    assert r.json()["reason"] == "not_authorized"


def test_day_scope_token_rejected_on_patient_endpoint(client):
    c, mp = client
    mp.setattr(main, "authorize", lambda u, p: AuthzDecision(True, u, p, "authorized"))
    r = _chat(c, {"Authorization": f"Bearer {_tok(101, scope='day')}"}, pid=101)
    assert r.status_code == 403


def test_cross_user_valid_token_but_not_patients_provider_is_403(client):
    """A valid token for user 5 on patient 101 — but 101's provider is not user 5.
    The ACL passed at mint; the per-patient SCOPE layer (authorize) still denies."""
    c, mp = client
    mp.setattr(main, "authorize",
               lambda u, p: AuthzDecision(False, u, p, "not_patient_provider"))
    r = _chat(c, {"Authorization": f"Bearer {_tok(101, sub=5)}"}, pid=101)
    assert r.status_code == 403
    assert r.json()["reason"] == "not_authorized"


def test_valid_token_passes_auth_and_reaches_agent(client, monkeypatch):
    """A well-formed, provider-scoped token clears BOTH layers and reaches the
    agent (stubbed here to isolate auth)."""
    from app.contracts import AgentResponse

    c, mp = client
    mp.setattr(main, "authorize", lambda u, p: AuthzDecision(True, u, p, "authorized"))
    mp.setattr(main, "run_turn",
               lambda ctx, text, hist, obs: AgentResponse(correlation_id="t", briefing=[]))
    r = _chat(c, {"Authorization": f"Bearer {_tok(101)}"}, pid=101)
    assert r.status_code == 200
