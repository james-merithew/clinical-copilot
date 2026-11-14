"""Read-only data access layer.

Defense in depth: the DB user itself is read-only (invariant #3), AND this layer
only ever issues parameterized SELECTs. There is no write path in the codebase.
Every query is patient-scoped by an explicit `pid` bind parameter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Row

from .config import Settings, get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s: Settings = get_settings()
        _engine = create_engine(
            s.sqlalchemy_url,
            pool_size=s.db_pool_size,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )

        # Gate 2: bound every statement so a slow join degrades instead of
        # hanging the request. MariaDB `max_statement_time` is in seconds.
        timeout_s = max(0.1, s.db_statement_timeout_ms / 1000.0)

        @event.listens_for(_engine, "connect")
        def _set_statement_timeout(dbapi_conn, _record):  # noqa: ANN001
            with dbapi_conn.cursor() as cur:
                cur.execute("SET SESSION max_statement_time=%s", (timeout_s,))

    return _engine


def reset_engine() -> None:
    """Test hook: drop the cached engine so settings changes take effect."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def query(sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a parameterized read-only SELECT and return rows as dicts.

    Guardrail: reject anything that isn't a single SELECT/WITH statement, so a
    bug upstream can never turn this into a write.
    """
    stripped = sql.lstrip().lower()
    if not (stripped.startswith("select") or stripped.startswith("with")):
        raise ValueError("db.query only permits SELECT/WITH statements")
    if ";" in sql.rstrip().rstrip(";"):
        raise ValueError("db.query does not permit multiple statements")

    with get_engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        rows: list[Row] = result.fetchall()
        return [dict(r._mapping) for r in rows]


def ping() -> bool:
    """Readiness probe: is the database reachable?"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
