"""POST /ingest route guards — the boundary checks run BEFORE the pipeline.

Failure modes guarded:
- feature-flag bypass: while ingestion is disabled the surface must not exist
  (404, not 403 — no information leak that the route is real);
- authorization bypass: an un-authorized user<->patient pair must be refused
  before any read or write (the Week 1 gateway, same fail-closed shape);
- resource abuse: oversized bodies (413) and non-PDF/PNG/JPEG payloads (415)
  are rejected before the pipeline ever runs.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import ingest_routes, main, session_token
from app.authz import AuthzDecision
from app.main import app

from _helpers import LAB_SIMPLE_PDF, MEDLIST_SIMPLE_PDF

# Known signing key for the token dependency (get_signing_key is patched to it,
# so no MariaDB is needed). Tokens are minted per-pid to satisfy the token<->pid
# binding; these tests exercise the guards AFTER binding (authz deny, size, type).
TEST_KEY = "test-signing-key-32-bytes-minimum-000"


def _auth(pid: int, scope: str = "patient"):
    tok = session_token.mint(1, "tester", pid, scope, "patients:demo", TEST_KEY, 900, int(time.time()))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def client_env(ingest_db, ingest_settings, monkeypatch):
    # Token verification reads the key via session_token.get_signing_key — pin it.
    monkeypatch.setattr(session_token, "get_signing_key", lambda: TEST_KEY)
    # enforce_patient() (in main) runs authorize() which normally hits MariaDB;
    # pin it allowed unless a test overrides.
    monkeypatch.setattr(
        main, "authorize",
        lambda user_id, pid: AuthzDecision(True, user_id, pid, "authorized"),
    )
    with TestClient(app) as client:
        yield client, monkeypatch


def _post(client, *, pid=101, doc_type="lab_report", content=LAB_SIMPLE_PDF,
          mimetype="application/pdf"):
    return client.post(
        "/ingest",
        data={"pid": str(pid), "doc_type": doc_type},
        files={"file": ("upload.pdf", content, mimetype)},
        headers=_auth(pid),
    )


def test_404_when_ingest_disabled(client_env):
    client, monkeypatch = client_env
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ingest_enabled", False)
    assert _post(client).status_code == 404
    assert client.get("/documents/1/meta", params={"pid": 101}, headers=_auth(101)).status_code == 404
    assert client.get("/documents/1/citations", params={"pid": 101}, headers=_auth(101)).status_code == 404


def test_403_wrong_patient(client_env):
    client, monkeypatch = client_env
    monkeypatch.setattr(
        main, "authorize",
        lambda user_id, pid: AuthzDecision(False, user_id, pid, "not_patient_provider"),
    )
    resp = _post(client, pid=2)
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "not_authorized"
    assert "correlation_id" in body


def test_413_oversized_file(client_env):
    client, monkeypatch = client_env
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ingest_max_file_mb", 0)  # cap = 0 bytes
    resp = _post(client)
    assert resp.status_code == 413


def test_415_bad_mimetype(client_env):
    client, _ = client_env
    resp = _post(client, content=b"hello world", mimetype="text/plain")
    assert resp.status_code == 415
    resp = _post(client, content=b"GIF89a", mimetype="image/gif")
    assert resp.status_code == 415


def test_422_unknown_doc_type(client_env):
    client, _ = client_env
    assert _post(client, doc_type="discharge_summary").status_code == 422
    # near-miss spellings of the third doc type are still garbage
    assert _post(client, doc_type="med_list").status_code == 422


def test_medication_list_doc_type_accepted_end_to_end(client_env):
    """FAILURE MODE GUARDED: the route accepting medication_list but the
    result contract rejecting it (IngestResult.doc_type Literal) would 500 on
    every med-list upload — the doc_type must round-trip through the whole
    endpoint."""
    client, monkeypatch = client_env
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "mock_llm", True)  # hermetic canned extraction
    resp = _post(client, doc_type="medication_list", content=MEDLIST_SIMPLE_PDF)
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_type"] == "medication_list"
    # mock extraction = honest gaps, nothing persisted -> needs_review
    assert body["status"] == "needs_review"
    assert body["persisted"] == []


def test_document_endpoints_404_for_wrong_patient(client_env, ingest_db):
    """A document id is not a capability: pid mismatch -> 404, no bytes leak."""
    client, _ = client_env
    from app.ingest import writer

    with ingest_db.begin() as conn:
        doc_id = writer.store_document_fallback(
            conn, 101, LAB_SIMPLE_PDF, "application/pdf", "cd" * 64, "lab_report"
        )
    # right patient: meta works
    ok = client.get(f"/documents/{doc_id}/meta", params={"pid": 101}, headers=_auth(101))
    assert ok.status_code == 200
    assert ok.json()["page_count"] == 1
    # wrong (but authorized) patient: the document is not theirs -> 404
    wrong = client.get(f"/documents/{doc_id}/meta", params={"pid": 102}, headers=_auth(102))
    assert wrong.status_code == 404
    page = client.get(f"/documents/{doc_id}/page/1.png", params={"pid": 101}, headers=_auth(101))
    assert page.status_code == 200
    assert page.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(f"/documents/{doc_id}/page/99.png", params={"pid": 101}, headers=_auth(101)).status_code == 404


def test_page_warm_cache_still_enforces_ownership(client_env, ingest_db):
    """Regression (cross-patient PHI leak): the rendered-page LRU cache must NOT
    bypass the document<->patient ownership check on a warm hit. Two patients of
    the SAME (server-pinned) provider both pass user<->patient authz, so the
    per-document ownership check is the only thing separating them — and it must
    run on EVERY request, even when the page PNG is already cached.

    Sequence: owner (pid 101) warms the cache, then a different authorized
    patient (pid 102) who does NOT own the document must still 404, not receive
    patient 101's rendered lab page from the warm cache."""
    client, _ = client_env
    from app import ingest_routes
    from app.ingest import writer

    ingest_routes._PAGE_CACHE.clear()
    with ingest_db.begin() as conn:
        doc_id = writer.store_document_fallback(
            conn, 101, LAB_SIMPLE_PDF, "application/pdf", "ef" * 64, "lab_report"
        )
    # Owner warms the cache for (doc_id, page=1, scale=2.0).
    owner = client.get(f"/documents/{doc_id}/page/1.png", params={"pid": 101}, headers=_auth(101))
    assert owner.status_code == 200
    assert owner.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert (doc_id, 1, 2.0) in ingest_routes._PAGE_CACHE  # cache is now warm

    # Non-owner (authorized user, but not this patient's document) must 404 even
    # on the warm-cache path — no bytes may leak.
    leak = client.get(f"/documents/{doc_id}/page/1.png", params={"pid": 102}, headers=_auth(102))
    assert leak.status_code == 404
