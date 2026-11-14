"""Least-privilege write layer for the deterministic Ingestion Service (D2).

This is the ONLY module in the codebase that writes to the database, and it
is never reachable from a model tool call — the LLM still has no DB handle
(CLAUDE.md invariant 1/3). Defense in depth, enforced twice:

1. **DB grants**: a SEPARATE `copilot_ingest` MariaDB user (INSERT-only on the
   OpenEMR lab-chain tables + `lists`, flag-gated `documents` fallback,
   `sequences` UPDATE for OpenEMR's id-allocation idiom, ALL on `copilot.*`,
   SELECT for dedup/id-recovery reads). The Week 1 read path (`db.py`,
   `copilot_ro`) is untouched.
2. **In-code allowlist** (`_ALLOWED_VERBS`): every statement passes through
   `execute_write`, which parses the verb + target table and rejects anything
   not explicitly allowed — so a bug in one layer is caught by the other.

The engine is built from `settings.ingest_sqlalchemy_url` and is SEPARATE
from the read-only engine. Tests inject an in-memory SQLite engine (with an
ATTACHed `copilot` schema so the qualified table names match production SQL).
"""

from __future__ import annotations

import base64
import datetime as _dt
import logging
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from ..config import get_settings
from ..correlation import log_event

log = logging.getLogger("copilot.ingest.writer")

# table -> verbs this codebase is allowed to run against it. Everything else
# (any UPDATE/DELETE on clinical tables, any other table at all) is rejected
# in code even if a grant existed.
_ALLOWED_VERBS: dict[str, frozenset[str]] = {
    "procedure_order": frozenset({"INSERT"}),
    "procedure_order_code": frozenset({"INSERT"}),
    "procedure_report": frozenset({"INSERT"}),
    "procedure_result": frozenset({"INSERT"}),
    "lists": frozenset({"INSERT"}),
    # fallback path only — additionally gated on settings.ingest_db_doc_fallback
    # at the call site (store_document_fallback):
    "documents": frozenset({"INSERT"}),
    "sequences": frozenset({"UPDATE"}),  # OpenEMR LAST_INSERT_ID id idiom
    # sidecar-owned schema (full access per grants; only INSERT is used):
    "copilot.ingestion_run": frozenset({"INSERT", "UPDATE", "DELETE"}),
    "copilot.provenance": frozenset({"INSERT", "UPDATE", "DELETE"}),
    "copilot.intake_fact": frozenset({"INSERT", "UPDATE", "DELETE"}),
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

# Real lab reports print the collection date in many formats; MariaDB `datetime`
# columns only accept a canonical form. We parse the extracted date to a
# canonical `YYYY-MM-DD` before persist. An unparseable string is NEVER injected
# into a datetime column (that would 1292-error on strict MariaDB or silently
# store a zero date on non-strict) — it becomes NULL and the raw value is kept
# in provenance/report_notes instead.
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",  # 2026-07-10 (ISO, already canonical)
    "%Y/%m/%d",  # 2026/07/10
    "%m/%d/%Y",  # 07/10/2026 (US)
    "%m-%d-%Y",  # 07-10-2026 (US)
    "%d-%b-%Y",  # 10-Jul-2026
    "%d-%B-%Y",  # 10-July-2026
    "%b %d, %Y",  # Jul 10, 2026
    "%B %d, %Y",  # July 10, 2026
    "%d %b %Y",  # 10 Jul 2026
    "%d %B %Y",  # 10 July 2026
    "%d.%m.%Y",  # 10.07.2026 (EU dotted)
    "%m/%d/%y",  # 07/10/26
)


def normalize_collection_date(raw: str | None) -> str | None:
    """Parse an extracted lab collection date to canonical `YYYY-MM-DD`.

    Returns None when `raw` is empty or cannot be parsed by any known format —
    the caller must then write NULL into the datetime column (never the raw
    string) and surface the unparsed value as a gap / in report_notes.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Tolerate a trailing time component ("2026-07-10 08:30") by taking the
    # leading date token for the canonical date columns.
    head = s.split("T")[0].split()[0] if ("T" in s or " " in s) else s
    for candidate in (s, head):
        for fmt in _DATE_FORMATS:
            try:
                return _dt.datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


class WriterViolation(RuntimeError):
    """A statement outside the in-code allowlist was attempted."""


# ---- engine ------------------------------------------------------------------

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_engine(
            s.ingest_sqlalchemy_url,
            pool_size=2,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Test hook: inject an engine (in-memory SQLite) or reset with None."""
    global _engine
    if _engine is not None and engine is not _engine:
        try:
            _engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    _engine = engine


def reset_engine() -> None:
    set_engine(None)


def ping() -> bool:
    """Readiness probe for the ingest DB identity. Never raises."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- guarded statement execution ----------------------------------------------


def _target_table(sql: str) -> tuple[str, str]:
    """Parse (VERB, table) out of a single write statement."""
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        raise WriterViolation("multiple statements are not permitted")
    tokens = stripped.split()
    if len(tokens) < 3:
        raise WriterViolation("unparseable write statement")
    verb = tokens[0].upper()
    if verb == "INSERT":
        if tokens[1].upper() != "INTO":
            raise WriterViolation("only INSERT INTO is permitted")
        table = tokens[2]
    elif verb == "UPDATE":
        table = tokens[1]
    elif verb == "DELETE":
        if tokens[1].upper() != "FROM":
            raise WriterViolation("only DELETE FROM is parseable")
        table = tokens[2]
    else:
        raise WriterViolation(f"verb {verb} is not permitted")
    table = table.strip("`").split("(")[0].strip()
    if not _TABLE_RE.match(table):
        raise WriterViolation("unparseable target table")
    return verb, table


def execute_write(conn: Connection, sql: str, params: Mapping[str, Any] | None = None):
    """The single chokepoint for every write statement. Rejects any verb/table
    combination not on the in-code allowlist (independent of DB grants)."""
    verb, table = _target_table(sql)
    allowed = _ALLOWED_VERBS.get(table)
    if allowed is None or verb not in allowed:
        raise WriterViolation(f"{verb} on {table} is not on the writer allowlist")
    return conn.execute(text(sql), params or {})


def read_rows(conn: Connection, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
    """Parameterized SELECT on the ingest connection (dedup / id recovery /
    demographics compare — the SELECT grant exists for exactly this)."""
    if not sql.lstrip().lower().startswith("select"):
        raise WriterViolation("read_rows only permits SELECT statements")
    result = conn.execute(text(sql), params or {})
    return [dict(r._mapping) for r in result.fetchall()]


def insert_row(conn: Connection, table: str, values: Mapping[str, Any]) -> int:
    """Build + run a parameterized INSERT (columns validated as identifiers),
    returning the autoincrement PK where the dialect provides one."""
    if not values:
        raise WriterViolation("insert_row requires at least one column")
    for col in values:
        if not _IDENT_RE.match(col):
            raise WriterViolation(f"invalid column identifier: {col!r}")
    cols = ", ".join(f"`{c}`" for c in values)
    binds = ", ".join(f":{c}" for c in values)
    result = execute_write(conn, f"INSERT INTO {table} ({cols}) VALUES ({binds})", dict(values))
    pk = getattr(result, "lastrowid", None)
    return int(pk) if pk else 0


# ---- documents: id recovery + flag-gated fallback ------------------------------


def find_document_id_by_hash(
    conn: Connection, sha3_hex: str, pid: int
) -> int | None:
    """Recover `documents.id` by OpenEMR's own sha3-512 content hash, scoped to
    the OWNING patient (`foreign_id = pid`).

    Dedup/recovery MUST be per-(pid, hash): the same bytes uploaded for a second
    patient get their OWN `documents` row (foreign_id = that pid). Matching on
    hash alone would (a) reuse patient A's row for patient B, cross-linking two
    charts to one document, and (b) collide on `UNIQUE(document_id, field_id)` in
    copilot.provenance, hard-failing the whole ingest for patient B.
    """
    rows = read_rows(
        conn,
        "SELECT id FROM documents WHERE hash = :h AND foreign_id = :pid "
        "AND deleted = 0 ORDER BY id DESC LIMIT 1",
        {"h": sha3_hex, "pid": pid},
    )
    return int(rows[0]["id"]) if rows else None


def _allocate_document_id(conn: Connection) -> int:
    """OpenEMR's `sequences` id idiom. documents.id is NOT auto-increment.
    Dialect-aware so the hermetic SQLite tests exercise the same path."""
    if conn.dialect.name == "mysql":
        execute_write(conn, "UPDATE sequences SET id = LAST_INSERT_ID(id + 1)")
        rows = read_rows(conn, "SELECT LAST_INSERT_ID() AS id")
    else:
        execute_write(conn, "UPDATE sequences SET id = id + 1")
        rows = read_rows(conn, "SELECT id FROM sequences")
    return int(rows[0]["id"])


def store_document_fallback(
    conn: Connection,
    pid: int,
    file_bytes: bytes,
    mimetype: str,
    sha3_hex: str,
    doc_type: str,
) -> int:
    """Direct `documents` row with bytes in `document_data` — ONLY behind
    COPILOT_INGEST_DB_DOC_FALLBACK (the oauth_keys-gap escape hatch, D2).
    The stored name is the generic doc_type, never the uploaded filename
    (filenames routinely contain patient names)."""
    if not get_settings().ingest_db_doc_fallback:
        raise WriterViolation(
            "direct documents insert requires COPILOT_INGEST_DB_DOC_FALLBACK"
        )
    doc_id = _allocate_document_id(conn)
    now = _dt.datetime.now()
    ext = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}.get(mimetype, "")
    insert_row(
        conn,
        "documents",
        {
            "id": doc_id,
            "type": "blob",
            "size": len(file_bytes),
            "date": now.strftime("%Y-%m-%d %H:%M:%S"),
            "url": f"copilot://ingest/{sha3_hex[:16]}{ext}",
            "mimetype": mimetype,
            "docdate": now.strftime("%Y-%m-%d"),
            "hash": sha3_hex,
            "name": f"{doc_type}{ext}",
            "foreign_id": pid,
            "storagemethod": 0,
            # `revision` is NOT NULL with no default on some OpenEMR schema
            # versions (it defaults to CURRENT_TIMESTAMP on others) — set it
            # explicitly so the INSERT is portable across both.
            "revision": now.strftime("%Y-%m-%d %H:%M:%S"),
            "document_data": base64.standard_b64encode(file_bytes).decode("ascii"),
        },
    )
    log_event(log, "ingest_doc_fallback", document_id=doc_id, pid=pid, doc_type=doc_type)
    return doc_id


def load_document_row(conn: Connection, document_id: int) -> dict | None:
    rows = read_rows(
        conn,
        "SELECT id, foreign_id, mimetype, document_data, url, name, hash "
        "FROM documents WHERE id = :id AND deleted = 0 LIMIT 1",
        {"id": document_id},
    )
    return rows[0] if rows else None


# ---- the lab persistence chain (single transaction, managed by the service) ----


def persist_lab_chain(
    conn: Connection,
    pid: int,
    extraction,  # noqa: ANN001 - LabReportExtraction
    document_id: int,
    correlation_id: str,
    prompt_version: str,
) -> list[dict]:
    """INSERT the native OpenEMR lab chain for every VERIFIED analyte:
    procedure_order -> procedure_order_code -> procedure_report ->
    procedure_result. Returns per-row facts for provenance + citations.

    Only rows whose name AND value survived text-layer verification are
    written; unit/range/flag are included only when themselves verified
    (a missing range is a stated gap, never an invented one). The
    correlation id + prompt version ride in procedure_report.report_notes so
    every row is traceable to its ingestion trace.
    """
    import json

    verified = [
        lv
        for lv in extraction.lab_values
        if lv.name.status == "extracted" and lv.value.status == "extracted"
    ]
    if not verified:
        return []

    coll_raw = (
        extraction.collection_date.value
        if extraction.collection_date.status == "extracted"
        else None
    )
    # Never let a document-format date string reach a MariaDB datetime column.
    coll = normalize_collection_date(coll_raw)
    date_unparsed = coll_raw is not None and coll is None
    if date_unparsed:
        log_event(
            log, "ingest_date_unparsed",
            field_id="collection_date", target_table="procedure_order",
        )
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    order_id = insert_row(
        conn,
        "procedure_order",
        {
            "patient_id": pid,
            "provider_id": 0,
            "encounter_id": 0,
            "date_ordered": coll or now,
            "date_collected": coll,
            "order_status": "complete",
            "activity": 1,
            "procedure_order_type": "laboratory_test",
        },
    )
    insert_row(
        conn,
        "procedure_order_code",
        {
            "procedure_order_id": order_id,
            "procedure_order_seq": 1,
            "procedure_code": "COPILOT-INGEST",
            "procedure_name": "Ingested lab report",
        },
    )
    report_id = insert_row(
        conn,
        "procedure_report",
        {
            "procedure_order_id": order_id,
            "procedure_order_seq": 1,
            "date_collected": coll,
            "date_report": now,
            "report_status": "final",
            "review_status": "received",
            "report_notes": json.dumps(
                {
                    "source": "copilot-ingest",
                    "correlation_id": correlation_id,
                    "prompt_version": prompt_version,
                    "document_id": document_id,
                    # keep the verbatim printed date for audit even when it could
                    # not be parsed into the canonical datetime columns
                    "collection_date_raw": coll_raw,
                    "collection_date_parsed": coll,
                }
            ),
        },
    )

    facts: list[dict] = []
    for lv in verified:
        value = str(lv.value.value)
        is_num = bool(re.fullmatch(r"[<>=]?\s*-?\d+(\.\d+)?", value))
        result_pk = insert_row(
            conn,
            "procedure_result",
            {
                "procedure_report_id": report_id,
                "result_data_type": "N" if is_num else "S",
                "result_code": lv.field_id[:31],
                "result_text": str(lv.name.value)[:255],
                "date": coll,
                "units": (str(lv.unit.value)[:31] if lv.unit.status == "extracted" else ""),
                "result": value[:255],
                "range": (
                    str(lv.reference_range.value)[:255]
                    if lv.reference_range.status == "extracted"
                    else ""
                ),
                "abnormal": (
                    "yes"
                    if lv.abnormal_flag.status == "extracted" and bool(lv.abnormal_flag.value)
                    else ""
                ),
                "document_id": document_id,
                "result_status": "final",
            },
        )
        facts.append(
            {
                "field_id": lv.field_id,
                "table": "procedure_result",
                "pk": result_pk,
                "label": f"{lv.name.value} {coll or ''}".strip(),
                "envelope": lv.value,
            }
        )
        log_event(
            log, "ingest_row_written",
            target_table="procedure_result", target_pk=result_pk, field_id=lv.field_id,
        )
    return facts


# A document-REPORTED med status that means "no longer taking". Anything else
# (Active, unknown, unverified) defaults to active=1 — the conservative read
# for a surface the chart's own med list already treats as testimony.
_MED_INACTIVE_STATUSES = frozenset({"discontinued", "stopped", "held", "inactive"})


def persist_medication_list(
    conn: Connection,
    pid: int,
    extraction,  # noqa: ANN001 - MedicationListExtraction
    document_id: int,
) -> list[dict]:
    """Med-list rows -> `lists` (`type='medication'`) — the SAME surface as
    intake current-meds, NEVER `prescriptions`: a document-reported med must
    not gain prescriber authority (tools/medications.py treats `prescriptions`
    as the ordered-authority row; AUDIT.md divergence rule).

    Only rows whose NAME survived text-layer verification are written; the
    other subfields ride along in `comments` only when THEMSELVES verified
    (a failed strength is a stated gap, never a persisted value). A verified
    status casefolding into the inactive set writes activity=0."""
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    list_date = (
        normalize_collection_date(extraction.list_date.value)
        if extraction.list_date.status == "extracted"
        else None
    )
    facts: list[dict] = []
    for row in extraction.medications:
        if row.name.status != "extracted":
            continue
        comments = " ".join(
            str(env.value)
            for env in (row.strength, row.route, row.frequency, row.prescriber)
            if env.status == "extracted"
        )
        inactive = (
            row.status.status == "extracted"
            and str(row.status.value).strip().casefold() in _MED_INACTIVE_STATUSES
        )
        pk = insert_row(
            conn,
            "lists",
            {
                "date": now,
                "type": "medication",
                "title": str(row.name.value)[:255],
                "begdate": list_date or now,
                "activity": 0 if inactive else 1,
                "pid": pid,
                "user": "copilot_ingest",
                "comments": comments,
            },
        )
        facts.append(
            {
                "field_id": row.field_id,
                "table": "lists",
                "pk": pk,
                "label": f"Med list: {row.name.value}",
                "envelope": row.name,
            }
        )
        log_event(
            log, "ingest_row_written",
            target_table="lists", target_pk=pk, field_id=row.field_id,
            document_id=document_id,
        )
    return facts


def persist_intake_lists(
    conn: Connection,
    pid: int,
    extraction,  # noqa: ANN001 - IntakeFormExtraction
) -> list[dict]:
    """Meds/allergies -> `lists` (the table OpenEMR itself uses). Only rows
    whose NAME verified are written; the detail rides along only if verified."""
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    facts: list[dict] = []
    for kind, rows, list_type in (
        ("medication", extraction.medications, "medication"),
        ("allergy", extraction.allergies, "allergy"),
    ):
        for row in rows:
            if row.name.status != "extracted":
                continue
            detail = str(row.detail.value) if row.detail.status == "extracted" else ""
            values: dict[str, Any] = {
                "date": now,
                "type": list_type,
                "title": str(row.name.value)[:255],
                "begdate": now,
                "activity": 1,
                "pid": pid,
                "user": "copilot_ingest",
            }
            if list_type == "allergy":
                values["reaction"] = detail[:255]
            else:
                values["comments"] = detail
            pk = insert_row(conn, "lists", values)
            facts.append(
                {
                    "field_id": row.field_id,
                    "table": "lists",
                    "pk": pk,
                    "label": f"Intake {kind}: {row.name.value}",
                    "envelope": row.name,
                }
            )
            log_event(
                log, "ingest_row_written",
                target_table="lists", target_pk=pk, field_id=row.field_id,
            )
    return facts
