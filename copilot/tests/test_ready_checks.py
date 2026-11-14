"""/ready Week-2 dependency sub-checks (assignment p7 + grader feedback).

The rubric requires /ready to report document_storage, vector_index, and
reranker reachability, returning a *degraded* status when an ancillary
dependency is unavailable — never a binary up/down flip.

Failure modes guarded:
- rubric checks (document_storage / vector_index / reranker) missing from the
  /ready payload — the exact gap the grader flagged;
- an ancillary dependency outage flipping /ready to 503 (ancillary problems
  must be `degraded` + 200; chat continues);
- a core outage (DB) reported as anything but 503 `down`;
- `document_storage` leaking into the payload when ingest is disabled (gating
  parity with openemr_api / ingest_db);
- probe functions doing expensive work (an index build) or raising.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import get_settings
from app.main import app
from app.rag import index as rag_index
from app.rag import providers as rag_providers
from app.rag.providers import reranker_ready


@pytest.fixture
def ready_env(monkeypatch):
    """Hermetic /ready: core probes green, mock LLM, ingest off by default."""
    monkeypatch.setenv("COPILOT_MOCK_LLM", "true")
    # setenv (not delenv): real env vars outrank the developer's .env file.
    monkeypatch.setenv("COPILOT_INGEST_ENABLED", "false")
    # These tests verify the DETAILED /ready payload (the Week-2 rubric checks), so run
    # with public detail on. The Week-3 hardening (anonymous callers get bare status) is
    # covered separately below.
    monkeypatch.setenv("COPILOT_READY_PUBLIC_DETAIL", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(main.db, "ping", lambda: True)
    monkeypatch.setattr(main, "ls_ready", lambda: True)
    try:
        yield monkeypatch
    finally:
        get_settings.cache_clear()


def _get_ready():
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.get("/ready")


# ---- payload shape ---------------------------------------------------------------


def test_ready_reports_rubric_checks(ready_env):
    """vector_index and reranker are always present; all green -> ready."""
    r = _get_ready()
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["status"] == "ready"
    for key in ("database", "llm", "observability", "vector_index", "reranker"):
        assert key in body["checks"], f"missing rubric check: {key}"
        assert body["checks"][key] is True


def test_document_storage_gated_on_ingest(ready_env):
    """Ingest disabled -> no document_storage key (parity with openemr_api)."""
    body = _get_ready().json()
    assert "document_storage" not in body["checks"]
    assert "openemr_api" not in body["checks"]


def test_document_storage_present_when_ingest_enabled(ready_env):
    ready_env.setenv("COPILOT_INGEST_ENABLED", "true")
    get_settings.cache_clear()
    from app.ingest import openemr_api as ingest_api
    from app.ingest import writer as ingest_writer

    ready_env.setattr(ingest_api, "ping", lambda settings=None: True)
    ready_env.setattr(ingest_writer, "ping", lambda: True)
    # Storage path unconfigured -> False, and that is DEGRADED (200), never down.
    ready_env.setattr(ingest_api, "document_storage_ready", lambda s: False)
    body = _get_ready().json()
    assert "document_storage" in body["checks"]
    assert body["checks"]["document_storage"] is False
    assert body["status"] == "degraded"
    assert body["ready"] is True


# ---- degraded vs down ------------------------------------------------------------


def test_vector_index_failure_is_degraded_not_down(ready_env):
    ready_env.setattr(rag_index, "vector_index_ready", lambda: False)
    r = _get_ready()
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["vector_index"] is False
    assert body["status"] == "degraded"
    assert body["ready"] is True


def test_reranker_failure_is_degraded_not_down(ready_env):
    ready_env.setattr(rag_providers, "reranker_ready", lambda s: False)
    r = _get_ready()
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["reranker"] is False
    assert body["status"] == "degraded"


def test_core_db_failure_is_still_503_down(ready_env):
    """A core check failing must stay 503 down, new checks notwithstanding."""
    ready_env.setattr(main.db, "ping", lambda: False)
    ready_env.setattr(rag_index, "vector_index_ready", lambda: False)
    r = _get_ready()
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["status"] == "down"


# ---- probe functions -------------------------------------------------------------


def test_vector_index_ready_true_on_committed_corpus():
    assert rag_index.vector_index_ready() is True


def test_vector_index_ready_false_on_missing_or_empty_corpus(tmp_path, monkeypatch):
    from app.rag import corpus as corpus_mod

    monkeypatch.setattr(corpus_mod, "DEFAULT_CORPUS_DIR", tmp_path / "nope")
    assert rag_index.vector_index_ready() is False
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(corpus_mod, "DEFAULT_CORPUS_DIR", empty)
    assert rag_index.vector_index_ready() is False


def test_vector_index_ready_never_builds_the_index(monkeypatch):
    """The probe must not pay the lazy-build cost (startup invariant)."""

    def boom() -> None:  # pragma: no cover - failure branch
        raise AssertionError("vector_index_ready must not call get_index()")

    monkeypatch.setattr(rag_index, "get_index", boom)
    assert rag_index.vector_index_ready() is True


def _settings(**kw):
    """Settings isolated from the developer's real .env / environment (same
    pattern as test_rag_providers.make_settings): explicit kwargs win."""
    from app.config import Settings

    base = dict(
        _env_file=None,
        cohere_api_key="",
        anthropic_api_key="",
        mock_llm=False,
        embed_provider="auto",
        rerank_provider="auto",
        openemr_base_url="",
        openemr_oauth_client_id="",
        ingest_db_doc_fallback=False,
    )
    base.update(kw)
    return Settings(**base)


def test_reranker_ready_mock_mode_true():
    assert reranker_ready(_settings(mock_llm=True)) is True


def test_reranker_ready_keyed_provider():
    assert reranker_ready(_settings(cohere_api_key="k")) is True
    assert reranker_ready(_settings(anthropic_api_key="k")) is True


def test_reranker_ready_noop_fallback_is_false():
    """No key + auto ladder -> Noop -> reranking unavailable -> not ready
    (surfaces as degraded; RRF-only is the honest fallback, not full health)."""
    assert reranker_ready(_settings()) is False
    assert reranker_ready(_settings(cohere_api_key="k", rerank_provider="noop")) is False


def test_document_storage_ready_paths():
    from app.ingest.openemr_api import document_storage_ready

    assert document_storage_ready(_settings()) is False
    assert document_storage_ready(_settings(ingest_db_doc_fallback=True)) is True
    assert (
        document_storage_ready(
            _settings(
                openemr_base_url="http://localhost:8300",
                openemr_oauth_client_id="cid",
            )
        )
        is True
    )
