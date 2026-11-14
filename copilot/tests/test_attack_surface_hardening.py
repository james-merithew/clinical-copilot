"""Week-3 attack-surface hardening — remediation of the red-team unauth findings.

The adversarial platform found that an UNAUTHENTICATED caller could read the internal
component inventory from /ready and enumerate the whole API via /openapi.json + /docs.
These tests lock in the fixes so the findings can't silently regress:

- /ready returns a bare {ready, status} to anonymous callers (still 200/503) — the
  component `checks` map is disclosed only to an authorized caller or under the demo flag;
- /openapi.json, /docs, /redoc are disabled by default (COPILOT_EXPOSE_DOCS unset).

The full detailed-/ready behaviour (Week-2 rubric) is verified in test_ready_checks.py
with the demo flag on. The app is constructed with docs off because COPILOT_EXPOSE_DOCS
defaults false — no reload needed; /ready reads the settings live per request.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main, session_token
from app.config import get_settings
from app.main import app

TEST_KEY = "test-signing-key-attack-surface"


@pytest.fixture
def hardened_env(monkeypatch):
    """Default production posture: /ready detail not public, core probes green."""
    monkeypatch.setenv("COPILOT_MOCK_LLM", "true")
    monkeypatch.setenv("COPILOT_INGEST_ENABLED", "false")
    monkeypatch.delenv("COPILOT_READY_PUBLIC_DETAIL", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(main.db, "ping", lambda: True)
    monkeypatch.setattr(main, "ls_ready", lambda: True)
    monkeypatch.setattr(session_token, "get_signing_key", lambda: TEST_KEY)
    try:
        yield
    finally:
        get_settings.cache_clear()


def _token():
    return session_token.mint(
        1, "tester", 101, "patient", "patients:demo", TEST_KEY, 900, int(time.time())
    )


def test_ready_hides_component_inventory_from_anonymous(hardened_env):
    # fm (RT-005): /ready leaks the internal integration map to an unauthenticated caller
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "ready" in body and "status" in body   # the probe still works
    assert "checks" not in body                     # but the component map is withheld


def test_ready_reveals_detail_to_authorized_caller(hardened_env):
    # fm: the Week-2 detailed /ready must remain available to an authenticated caller
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/ready", headers={"Authorization": f"Bearer {_token()}"})
    body = r.json()
    assert "checks" in body and "vector_index" in body["checks"]


def test_ready_detail_public_when_demo_flag_set(hardened_env, monkeypatch):
    # fm: the demo escape hatch must still surface the full detail anonymously
    monkeypatch.setenv("COPILOT_READY_PUBLIC_DETAIL", "true")
    get_settings.cache_clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert "checks" in client.get("/ready").json()


def test_openapi_and_docs_disabled_by_default(hardened_env):
    # fm: /openapi.json + /docs let an anonymous caller enumerate the full attack surface
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
