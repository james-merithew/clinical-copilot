"""Correlation ID + PHI-safe structured logging.

Every request gets a unique correlation ID (assigned at /chat) that threads
through every log line, tool call, and LLM interaction so a full trace can be
reconstructed from logs alone (CLAUDE.md invariant 6, brief Eng-Req).

PHI discipline: we log correlation IDs, user_id, pid, source-table PKs, and
counts — NEVER raw PHI values (names, notes, results). If you are tempted to log
a value, log its SourceRef instead.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    return _correlation_id.get()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": get_correlation_id(),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


# PHI discipline is ENFORCED, not by convention: only these non-PHI keys are
# ever serialized to logs. Anything else (a name, a drug, note text) is dropped.
_ALLOWED_LOG_KEYS = frozenset({
    "user_id", "pid", "reason", "error", "path", "tool", "count",
    "verified", "quarantined", "constraint_flags", "latency_ms", "kind",
    "tool_calls", "tool_failures", "retries", "input_tokens", "output_tokens",
    "cost_usd", "verified_claims", "quarantined_claims", "verification_passed",
    "degraded", "steps", "db_configured", "anthropic_key_present",
    "langsmith_key_present", "bearer_secret_present", "mock_llm",
    # --- Week 2: ingestion pipeline (IDs, counts, enums — never values).
    # NOTE: `filename` is DELIBERATELY absent and must never be added —
    # uploaded filenames routinely contain patient names (PHI). Log the
    # document_id / content-hash dedup outcome instead.
    "doc_type", "document_id", "size_bytes", "page_count", "mimetype",
    "run_id", "phase", "dedup_hit", "fields_total", "fields_extracted",
    "fields_not_found", "fields_failed_verification", "target_table",
    "target_pk", "status", "field_id", "confidence",
    # --- Week 2: supervisor graph handoffs (enum/ID fields only — a Handoff's
    # `reason`/`payload_summary` free text has no key here, by design).
    "route", "from_node", "to_node", "seq", "handoff_id",
    # --- Week 2 extension: deterministic critic node (counts only — claim
    # text / chip labels never get a key here).
    "rejected_uncited", "stripped_unsafe",
    # --- Week 2: guideline retrieval.
    "retrieval_queries", "retrieval_returned", "retrieval_cited",
    "rerank_provider", "rerank_ms", "provider", "chunk_count", "hit",
    "retrieval_degraded",  # per-turn RAG-degradation flag (bool; provider/reranker above)
})


def _sanitize(fields: dict[str, object]) -> dict[str, object]:
    """Drop any key not on the allowlist; cap string lengths. Guarantees PHI
    values cannot reach the log sink even if a caller passes them by mistake."""
    clean: dict[str, object] = {}
    dropped = 0
    for k, v in fields.items():
        if k not in _ALLOWED_LOG_KEYS:
            dropped += 1
            continue
        if isinstance(v, str):
            clean[k] = v[:120]
        elif isinstance(v, (int, float, bool)) or v is None or isinstance(v, list):
            clean[k] = v
        else:
            clean[k] = type(v).__name__
    if dropped:
        clean["_dropped_fields"] = dropped
    return clean


def log_event(logger: logging.Logger, msg: str, **fields: object) -> None:
    """Emit a structured log line. Non-allowlisted fields are dropped (PHI-safe)."""
    logger.info(msg, extra={"fields": _sanitize(fields)})
