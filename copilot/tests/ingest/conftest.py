"""Fixtures for the hermetic ingestion tests (no API, no MariaDB).

- `ingest_db`: in-memory SQLite injected into the writer, with an ATTACHed
  `copilot` schema so the production-qualified table names
  (`copilot.provenance` etc.) run verbatim;
- `ingest_settings`: the settings singleton toggled into ingest mode with the
  DB-document fallback ON (so document storage is exercised hermetically) and
  every external dependency OFF.

Shared non-fixture helpers (DDL, StubVLM, payload builders) live in
`_helpers.py` (importable because pytest puts this directory on sys.path in
rootdir mode).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from _helpers import DDL

from app.config import get_settings
from app.ingest import writer


@pytest.fixture
def ingest_db():
    """In-memory SQLite injected as the writer engine, with the `copilot`
    schema ATTACHed so production-qualified SQL runs unchanged."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS copilot")
        for stmt in DDL:
            cur.execute(stmt)
        raw.commit()
    finally:
        raw.close()  # returns the (static) connection to the pool
    writer.set_engine(engine)
    yield engine
    writer.set_engine(None)


@pytest.fixture
def ingest_settings(monkeypatch):
    """Settings singleton in hermetic ingest mode: fallback document storage,
    no REST, no LangSmith, no bearer, real (non-mock) extraction path."""
    s = get_settings()
    monkeypatch.setattr(s, "ingest_enabled", True)
    monkeypatch.setattr(s, "ingest_db_doc_fallback", True)
    monkeypatch.setattr(s, "ingest_allow_unverified", False)
    monkeypatch.setattr(s, "mock_llm", False)
    monkeypatch.setattr(s, "extract_max_retries", 2)
    monkeypatch.setattr(s, "openemr_base_url", "")
    monkeypatch.setattr(s, "openemr_oauth_client_id", "")
    monkeypatch.setattr(s, "bearer_secret", "")
    monkeypatch.setattr(s, "langsmith_api_key", "")
    monkeypatch.setattr(s, "rate_limit_per_min", 10_000)
    return s
