"""The deterministic Ingestion Service — `attach_and_extract` (D2/D3).

Pipeline (one call per upload, correlation ID threaded end to end):
    sha3-512 dedup -> store document (OpenEMR REST primary; flag-gated DB
    fallback) -> set document_id on the extraction -> forced-tool VLM extract
    -> deterministic text-layer verification -> persist VERIFIED fields only
    (single transaction) -> provenance -> IngestResult.

Invariants enforced here:
- invoked by a FastAPI endpoint, never by a model tool call (the LLM cannot
  reach the write path);
- unverified values are never persisted (`ingest_allow_unverified=False` is
  the default and demo config);
- idempotency: UNIQUE(content_hash, pid) — a re-upload is an explicit dedup
  notice and zero new rows;
- a mid-persist failure rolls the whole transaction back (no partial chains);
- intake demographics are compare-and-flag ONLY (patient_data never written);
- no PHI in logs: correlation ids, field ids, counts, enums — never values,
  quotes, or the uploaded filename.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..config import get_settings
from ..contracts import DocumentRef, SourceRef
from ..correlation import log_event
from ..observability import TurnObserver
from . import extractor, provenance, writer
from .extractor import ExtractionInvalid, ExtractionUnavailable
from .openemr_api import OpenEMRClient, OpenEMRError
from .schemas import (
    ExtractedField,
    IngestResult,
    IntakeFormExtraction,
    LabReportExtraction,
    MedicationListExtraction,
    PROMPT_VERSION,
)
from .verify_extraction import VerifySummary, verify

log = logging.getLogger("copilot.ingest.service")


class IngestStoreError(Exception):
    """The document could not be stored (REST down, no fallback enabled)."""


def _doc_ref_for(document_id: int, field_id: str, env: ExtractedField, label: str) -> DocumentRef:
    return DocumentRef(
        document_id=document_id,
        page=env.page,
        field_id=field_id,
        bbox=env.bbox,
        quote=env.verbatim_quote,
        label=label,
    )


def _recover_document_id_fresh(sha3_hex: str, pid: int) -> int | None:
    """Recover documents.id on a FRESH connection with AUTOCOMMIT isolation.

    The document row is committed by OpenEMR on a separate connection during the
    REST upload; on MariaDB's default REPEATABLE READ, re-using the writer's open
    transaction would not see it (its snapshot predates the commit). A brand-new
    AUTOCOMMIT connection starts its read after the commit, so the row is visible.
    Kept as its own connection deliberately — the recovery read must not reuse
    the writer's open transaction.
    """
    engine = writer.get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as fresh:
        return writer.find_document_id_by_hash(fresh, sha3_hex, pid)


def _store_document(
    conn,  # noqa: ANN001
    pid: int,
    file_bytes: bytes,
    filename: str,
    mimetype: str,
    doc_type: str,
    sha3_hex: str,
    emr: OpenEMRClient | None,
) -> tuple[int, bool]:
    """Store the source document and return (documents.id, used_fallback).

    Order: reuse an existing row with the same hash; else OpenEMR REST (the
    front door — OpenEMR creates the row and computes the same sha3-512);
    else the flag-gated direct-row fallback. Fail closed otherwise."""
    s = get_settings()

    # Dedup/recovery is per-(pid, hash): the same bytes uploaded for a different
    # patient get their own documents row (foreign_id = that pid).
    existing = writer.find_document_id_by_hash(conn, sha3_hex, pid)
    if existing is not None:
        return existing, False

    client = emr
    if client is None and s.openemr_base_url and s.openemr_oauth_client_id:
        client = OpenEMRClient(settings=s)
    if client is not None:
        try:
            client.upload_document(pid, file_bytes, filename, mimetype, doc_type)
            # OpenEMR commits the documents row on its OWN connection. This
            # writer transaction opened its InnoDB REPEATABLE READ snapshot with
            # the miss-SELECT above, so that just-committed row is invisible to a
            # re-SELECT on `conn`. Recover the id on a FRESH connection whose
            # snapshot starts after the commit.
            doc_id = _recover_document_id_fresh(sha3_hex, pid)
            if doc_id is not None:
                return doc_id, False
            log_event(log, "ingest_failed", phase="store", error="id_recovery_miss")
        except OpenEMRError as exc:
            log_event(log, "ingest_failed", phase="store", error=type(exc).__name__)
            # fall through to the flag-gated fallback

    if s.ingest_db_doc_fallback:
        return (
            writer.store_document_fallback(conn, pid, file_bytes, mimetype, sha3_hex, doc_type),
            True,
        )
    raise IngestStoreError("document store unavailable and DB fallback disabled")


def _demographic_mismatches(conn, pid: int, extraction: IntakeFormExtraction) -> list[dict]:  # noqa: ANN001
    """Compare verified intake demographics against patient_data; return
    mismatch facts. NEVER writes patient_data — a human resolves discrepancies
    (the no-silent-overwrite proof point, D2)."""
    rows = writer.read_rows(
        conn,
        "SELECT fname, lname, DOB, phone_home FROM patient_data WHERE pid = :pid LIMIT 1",
        {"pid": pid},
    )
    if not rows:
        return []
    chart = rows[0]

    def _tokens(s: str) -> set[str]:
        return {t for t in "".join(c if c.isalnum() else " " for c in s.lower()).split() if t}

    def _digits(s: str) -> str:
        return "".join(c for c in s if c.isdigit())

    mismatches: list[dict] = []
    if extraction.patient_name.status == "extracted":
        chart_name = f"{chart.get('lname') or ''}, {chart.get('fname') or ''}"
        if _tokens(str(extraction.patient_name.value)) != _tokens(chart_name):
            mismatches.append(
                {
                    "field_id": "patient_name",
                    "value": str(extraction.patient_name.value),
                    "chart_value": chart_name,
                    "envelope": extraction.patient_name,
                }
            )
    if extraction.date_of_birth.status == "extracted":
        chart_dob = str(chart.get("DOB") or "")[:10]
        if _digits(str(extraction.date_of_birth.value)) != _digits(chart_dob):
            mismatches.append(
                {
                    "field_id": "date_of_birth",
                    "value": str(extraction.date_of_birth.value),
                    "chart_value": chart_dob,
                    "envelope": extraction.date_of_birth,
                }
            )
    if extraction.phone.status == "extracted":
        chart_phone = str(chart.get("phone_home") or "")
        if _digits(str(extraction.phone.value)) and _digits(str(extraction.phone.value)) != _digits(
            chart_phone
        ):
            mismatches.append(
                {
                    "field_id": "phone",
                    "value": str(extraction.phone.value),
                    "chart_value": chart_phone,
                    "envelope": extraction.phone,
                }
            )
    return mismatches


def attach_and_extract(
    pid: int,
    file_bytes: bytes,
    filename: str,
    mimetype: str,
    doc_type: str,
    correlation_id: str,
    *,
    client: Any | None = None,  # injectable VLM client (tests)
    emr: OpenEMRClient | None = None,  # injectable OpenEMR client (tests)
) -> IngestResult:
    """Run the full ingestion pipeline for one uploaded document."""
    s = get_settings()
    obs = TurnObserver(pid=pid, kind="ingest")
    sha3_hex = hashlib.sha3_512(file_bytes).hexdigest()
    log_event(
        log, "ingest_received",
        pid=pid, doc_type=doc_type, mimetype=mimetype, size_bytes=len(file_bytes),
    )

    try:
        engine = writer.get_engine()

        # -- 1. dedup (idempotency key: content_hash + pid) ---------------------
        with obs.step("dedup"), engine.connect() as conn:
            run = provenance.find_run(conn, sha3_hex, pid)
        if run is not None:
            log_event(log, "ingest_dedup_hit", dedup_hit=True, pid=pid,
                      document_id=run.get("document_id"))
            return IngestResult(
                correlation_id=correlation_id,
                doc_type=doc_type,  # type: ignore[arg-type]
                status="deduplicated",
                document_id=run.get("document_id"),
                deduplicated=True,
                notes="This exact document was already ingested for this patient; "
                "no new rows were written.",
            )

        # -- 2. store the source document ---------------------------------------
        try:
            with obs.step("store"), engine.begin() as conn:
                document_id, used_fallback = _store_document(
                    conn, pid, file_bytes, filename, mimetype, doc_type, sha3_hex, emr
                )
        except IngestStoreError:
            obs.error = "store_failed"
            return IngestResult(
                correlation_id=correlation_id,
                doc_type=doc_type,  # type: ignore[arg-type]
                status="failed",
                gaps=["document could not be stored (OpenEMR API unavailable)"],
                notes="Nothing was extracted or persisted.",
            )

        # -- 3. extract (forced tool; retry-on-invalid; never trusted) ----------
        try:
            with obs.step("extract"):
                extraction = extractor.extract(
                    file_bytes, mimetype, doc_type, client=client, settings=s
                )
        except (ExtractionInvalid, ExtractionUnavailable) as exc:
            obs.error = type(exc).__name__
            log_event(log, "ingest_failed", phase="extract", error=type(exc).__name__)
            return IngestResult(
                correlation_id=correlation_id,
                doc_type=doc_type,  # type: ignore[arg-type]
                status="failed",
                document_id=document_id,
                gaps=["extraction failed; no fields were persisted"],
                notes="The document is stored but no data was extracted from it.",
            )
        extraction.document_id = document_id  # service-assigned, never the VLM

        # -- 4. deterministic verification --------------------------------------
        with obs.step("verify"):
            summary: VerifySummary = verify(extraction, file_bytes, mimetype)

        # -- 5. persist verified fields only (single transaction) ---------------
        persisted_refs: list[Any] = []
        persisted_any = False
        if summary.no_text_layer and not s.ingest_allow_unverified:
            # Scan/no-text-layer: display-only. Nothing persisted (D3.6).
            pass
        elif summary.verified > 0 or (summary.no_text_layer and s.ingest_allow_unverified):
            with obs.step("persist"), engine.begin() as conn:
                if summary.no_text_layer and s.ingest_allow_unverified:
                    _mark_unverified_persistable(extraction)
                # Explicit three-way dispatch on the extraction type — a new
                # doc type must be routed here deliberately; a catch-all else
                # would send it into the intake path (AttributeError at best,
                # wrong-table writes at worst).
                if isinstance(extraction, LabReportExtraction):
                    facts = writer.persist_lab_chain(
                        conn, pid, extraction, document_id, correlation_id, PROMPT_VERSION
                    )
                    intake_facts = []
                elif isinstance(extraction, MedicationListExtraction):
                    # lists (type='medication') ONLY — never prescriptions: a
                    # document-reported med has no prescriber authority.
                    facts = writer.persist_medication_list(
                        conn, pid, extraction, document_id
                    )
                    intake_facts = []
                else:  # IntakeFormExtraction
                    facts = writer.persist_intake_lists(conn, pid, extraction)
                    intake_facts = _collect_intake_facts(conn, pid, extraction)
                if facts or intake_facts:
                    run_id = provenance.insert_run(
                        conn,
                        correlation_id=correlation_id,
                        pid=pid,
                        doc_type=doc_type,
                        content_hash=sha3_hex,
                        document_id=document_id,
                        status="persisted",
                        prompt_version=PROMPT_VERSION,
                        fields_total=summary.total,
                        fields_extracted=summary.verified,
                        fields_not_found=summary.not_found,
                        fields_failed_verification=summary.failed_verification,
                    )
                    for f in facts:
                        env: ExtractedField = f["envelope"]
                        provenance.insert_provenance(
                            conn,
                            run_id=run_id,
                            document_id=document_id,
                            field_id=f["field_id"],
                            target_table=f["table"],
                            target_pk=f["pk"],
                            envelope=env,
                        )
                        persisted_refs.append(
                            SourceRef(table=f["table"], primary_key=f["pk"], label=f["label"])
                        )
                        persisted_refs.append(
                            _doc_ref_for(document_id, f["field_id"], env, f["label"])
                        )
                    for fact in intake_facts:
                        env = fact["envelope"]
                        fact_pk = provenance.insert_intake_fact(
                            conn,
                            run_id=run_id,
                            document_id=document_id,
                            pid=pid,
                            kind=fact["kind"],
                            field_id=fact["field_id"],
                            value=fact["value"],
                            chart_value=fact.get("chart_value"),
                        )
                        provenance.insert_provenance(
                            conn,
                            run_id=run_id,
                            document_id=document_id,
                            field_id=fact["field_id"],
                            target_table="copilot.intake_fact",
                            target_pk=fact_pk,
                            envelope=env,
                        )
                        persisted_refs.append(
                            _doc_ref_for(document_id, fact["field_id"], env, fact["label"])
                        )
                    persisted_any = True

        # -- 6. shape the result card --------------------------------------------
        if persisted_any:
            status = "persisted"
        elif summary.no_text_layer:
            status = "needs_review"
        elif summary.verified == 0 and summary.failed_verification > 0:
            status = "failed"  # everything the VLM proposed failed verification
        else:
            status = "needs_review"  # nothing extractable — a human should look

        notes = []
        if summary.no_text_layer:
            notes.append(
                "No machine-readable text layer (scanned/image document): nothing "
                "was persisted; all fields need human review."
            )
        if status == "persisted" and summary.failed_verification:
            notes.append(
                f"{summary.failed_verification} field(s) failed text-layer "
                "verification and were NOT persisted."
            )
        if used_fallback:
            notes.append("Document stored via direct-DB fallback (OpenEMR REST unavailable).")

        obs.degraded = status != "persisted"
        log_event(
            log, "ingest_complete",
            pid=pid, doc_type=doc_type, document_id=document_id, status=status,
            fields_total=summary.total, fields_extracted=summary.verified,
            fields_not_found=summary.not_found,
            fields_failed_verification=summary.failed_verification,
        )
        return IngestResult(
            correlation_id=correlation_id,
            doc_type=doc_type,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            document_id=document_id,
            persisted=persisted_refs,
            gaps=summary.gaps,
            needs_review=summary.needs_review,
            fields_total=summary.total,
            fields_extracted=summary.verified,
            fields_not_found=summary.not_found,
            fields_failed_verification=summary.failed_verification,
            notes=(" ".join(notes) or None),
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, never partial
        obs.error = type(exc).__name__
        log_event(log, "ingest_failed", phase="persist", error=type(exc).__name__)
        return IngestResult(
            correlation_id=correlation_id,
            doc_type=doc_type,  # type: ignore[arg-type]
            status="failed",
            gaps=["ingestion failed; the transaction was rolled back — no rows written"],
            notes="Internal error during ingestion; nothing was persisted.",
        )
    finally:
        obs.finish()


def _mark_unverified_persistable(extraction) -> None:  # noqa: ANN001
    """ONLY under COPILOT_INGEST_ALLOW_UNVERIFIED=true (never the default or
    demo config): treat 'unreadable' fields as extractable for persistence.
    Confidence stays None and bboxes stay origin='vlm' — visibly approximate."""
    from .verify_extraction import iter_envelopes

    for _fid, _sub, env in iter_envelopes(extraction):
        if env.status == "unreadable" and env.value is not None:
            env.status = "extracted"


def _collect_intake_facts(conn, pid: int, extraction: IntakeFormExtraction) -> list[dict]:  # noqa: ANN001
    """Chief concern / family history -> intake_fact rows; verified
    demographics -> compare-and-flag mismatch rows."""
    facts: list[dict] = []
    for fid, kind in (("chief_concern", "chief_concern"), ("family_history", "family_history")):
        env: ExtractedField = getattr(extraction, fid)
        if env.status == "extracted":
            facts.append(
                {
                    "kind": kind,
                    "field_id": fid,
                    "value": str(env.value),
                    "envelope": env,
                    "label": f"Intake — {fid.replace('_', ' ')}",
                }
            )
    for m in _demographic_mismatches(conn, pid, extraction):
        facts.append(
            {
                "kind": "demographic_mismatch",
                "field_id": m["field_id"],
                "value": m["value"],
                "chart_value": m["chart_value"],
                "envelope": m["envelope"],
                "label": f"Intake demographics mismatch — {m['field_id'].replace('_', ' ')}",
            }
        )
    return facts
