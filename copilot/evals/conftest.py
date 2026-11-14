"""Shared fixtures for the DB-integration eval suite.

These tests run against the LIVE local OpenEMR MariaDB (the demo/showcase seed).
Patient identifiers (pid 101/102/23/3) are stable seed FIXTURES, but real row
primary keys (e.g. the Metformin prescriptions.id) are DISCOVERED AT RUNTIME by
querying — never hardcoded — so the suite survives a reseed.

If the database is unreachable the whole eval suite is skipped (not failed):
these are integration tests, gated on the documented live dependency.
"""

from __future__ import annotations

import pytest

from app.db import ping, query
from app.tools import ToolContext

DEMO_USER_ID = 1


@pytest.fixture(scope="session", autouse=True)
def _require_db():
    if not ping():
        pytest.skip("live OpenEMR MariaDB not reachable — eval suite requires it")


@pytest.fixture(scope="session")
def q():
    """Runtime query helper for discovering seeded primary keys."""
    return query


@pytest.fixture
def make_ctx():
    """Build an authorized ToolContext for a given pid (demo user pinned to 1)."""
    def _make(pid: int, user_id: int = DEMO_USER_ID) -> ToolContext:
        return ToolContext(correlation_id="eval-cid", user_id=user_id, pid=pid)

    return _make
