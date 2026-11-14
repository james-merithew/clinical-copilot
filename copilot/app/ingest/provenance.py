"""CRUD for the sidecar-owned `copilot` schema (D2): ingestion_run,
provenance, intake_fact.

- `copilot.ingestion_run` — one row per PERSISTED upload; UNIQUE(content_hash,
  pid) is the idempotency key: re-uploading the same file for the same patient
  is a no-op (zero new rows, explicit dedup notice). Written in the SAME
  transaction as the clinical rows, so a mid-persist failure rolls everything
  back together and the upload can be retried cleanly.
- `copilot.provenance` — field-level lineage: target_table/target_pk <->
  document/field/page/bbox/confidence/quote; UNIQUE(document_id, field_id).
  This is what `GET /documents/{id}/citations` renders.
- `copilot.intake_fact` — intake facts with no natural OpenEMR home (chief
  concern, family history) plus demographic MISMATCH flags (compare-and-flag;
  `patient_data` is never overwritten).

All statements route through writer.execute_write / insert_row, so the
in-code allowlist applies here too. DDL lives in the seeder SQL
(copilot/deploy/seeder/live_seed.sql, copilot/sql/seed_showcase.sql).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Connection

from ..contracts import BBox, DocumentRef
from ..correlation import log_event
from .schemas import ExtractedField
from .writer import insert_row, read_rows

log = logging.getLogger("copilot.ingest.provenance")


# ---- ingestion_run -------------------------------------------------------------


def find_run(conn: Connection, content_hash: str, pid: int) -> dict | None:
    rows = read_rows(
        conn,
        "SELECT run_id, correlation_id, pid, doc_type, content_hash, document_id, "
        "status, prompt_version FROM copilot.ingestion_run "
        "WHERE content_hash = :h AND pid = :pid LIMIT 1",
        {"h": content_hash, "pid": pid},
    )
    return rows[0] if rows else None


def insert_run(
    conn: Connection,
    *,
    correlation_id: str,
    pid: int,
    doc_type: str,
    content_hash: str,
    document_id: int | None,
    status: str,
    prompt_version: str,
    fields_total: int,
    fields_extracted: int,
    fields_not_found: int,
    fields_failed_verification: int,
) -> int:
    run_id = insert_row(
        conn,
        "copilot.ingestion_run",
        {
            "correlation_id": correlation_id,
            "pid": pid,
            "doc_type": doc_type,
            "content_hash": content_hash,
            "document_id": document_id,
            "status": status,
            "prompt_version": prompt_version,
            "fields_total": fields_total,
            "fields_extracted": fields_extracted,
            "fields_not_found": fields_not_found,
            "fields_failed_verification": fields_failed_verification,
        },
    )
    log_event(
        log, "ingestion_run_recorded",
        run_id=run_id, pid=pid, doc_type=doc_type, document_id=document_id, status=status,
        fields_total=fields_total, fields_extracted=fields_extracted,
        fields_not_found=fields_not_found,
        fields_failed_verification=fields_failed_verification,
    )
    return run_id


# ---- provenance ---------------------------------------------------------------


def insert_provenance(
    conn: Connection,
    *,
    run_id: int,
    document_id: int,
    field_id: str,
    target_table: str | None,
    target_pk: int | None,
    envelope: ExtractedField,
) -> int:
    bbox = envelope.bbox
    values: dict[str, Any] = {
        "run_id": run_id,
        "document_id": document_id,
        "field_id": field_id,
        "target_table": target_table,
        "target_pk": target_pk,
        "page": envelope.page,
        "bbox_x0": bbox.x0 if bbox else None,
        "bbox_y0": bbox.y0 if bbox else None,
        "bbox_x1": bbox.x1 if bbox else None,
        "bbox_y1": bbox.y1 if bbox else None,
        "bbox_origin": bbox.origin if bbox else None,
        "confidence": envelope.confidence,
        "quote": envelope.verbatim_quote,
    }
    return insert_row(conn, "copilot.provenance", values)


def list_citations(conn: Connection, document_id: int) -> list[DocumentRef]:
    """Every persisted-field citation for a document, as DocumentRefs — the
    payload behind GET /documents/{id}/citations and the bbox overlay."""
    rows = read_rows(
        conn,
        "SELECT field_id, target_table, target_pk, page, bbox_x0, bbox_y0, bbox_x1, "
        "bbox_y1, bbox_origin, confidence, quote FROM copilot.provenance "
        "WHERE document_id = :d ORDER BY provenance_id",
        {"d": document_id},
    )
    refs: list[DocumentRef] = []
    for r in rows:
        bbox = None
        if r["bbox_x0"] is not None and r["page"]:
            bbox = BBox(
                page=int(r["page"]),
                x0=float(r["bbox_x0"]),
                y0=float(r["bbox_y0"]),
                x1=float(r["bbox_x1"]),
                y1=float(r["bbox_y1"]),
                origin=(r["bbox_origin"] or "vlm"),
            )
        label = f"Document {document_id}"
        if r["page"]:
            label += f" p.{r['page']}"
        label += f" — {r['field_id']}"
        refs.append(
            DocumentRef(
                document_id=document_id,
                page=(int(r["page"]) if r["page"] else None),
                field_id=r["field_id"],
                bbox=bbox,
                quote=r["quote"],
                label=label,
            )
        )
    return refs


# ---- intake_fact ---------------------------------------------------------------


def insert_intake_fact(
    conn: Connection,
    *,
    run_id: int,
    document_id: int,
    pid: int,
    kind: str,  # chief_concern | family_history | demographic_mismatch
    field_id: str,
    value: str | None,
    chart_value: str | None = None,
) -> int:
    fact_id = insert_row(
        conn,
        "copilot.intake_fact",
        {
            "run_id": run_id,
            "document_id": document_id,
            "pid": pid,
            "kind": kind,
            "field_id": field_id,
            "value": value,
            "chart_value": chart_value,
        },
    )
    log_event(
        log, "intake_fact_recorded",
        run_id=run_id, document_id=document_id, pid=pid, kind=kind, field_id=field_id,
    )
    return fact_id
