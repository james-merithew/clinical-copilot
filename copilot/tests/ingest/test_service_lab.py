"""attach_and_extract, lab path — the fail-closed persistence invariants (D2/D3).

Every test here guards a named failure mode:
- happy path: verified fields must land in the native lab chain WITH the
  document link + provenance (otherwise citations/click-to-source are lies);
- idempotency: a double upload silently duplicating labs would corrupt trends;
- invented value / value-not-in-quote: the VLM hallucination paths — nothing
  unverifiable may reach the database;
- missing reference range: a gap must stay a gap while the rest persists
  (graceful degradation, not all-or-nothing);
- scanned document: no text layer -> nothing persisted, everything needs
  review;
- schema-invalid extraction: explicit failure, zero writes;
- mid-persist crash: the transaction must roll back whole (no partial chains).
"""

from __future__ import annotations

import pytest

from app.contracts import DocumentRef, SourceRef
from app.ingest import provenance, service, writer

from _helpers import (
    LAB_SCANNED_PDF,
    LAB_SIMPLE_PDF,
    StubVLM,
    count,
    lab_payload,
    lab_row_quote,
    GROUND_TRUTH,
)

_N_ANALYTES = len(GROUND_TRUTH["lab_simple.pdf"]["analytes"])  # 10


def _run(engine, payloads, pdf=LAB_SIMPLE_PDF, pid=101, cid="cid-lab-1"):
    return service.attach_and_extract(
        pid=pid,
        file_bytes=pdf,
        filename="lab_simple.pdf",
        mimetype="application/pdf",
        doc_type="lab_report",
        correlation_id=cid,
        client=StubVLM(payloads),
    )


def test_happy_path_persists_verified_lab_chain(ingest_db, ingest_settings):
    result = _run(ingest_db, [lab_payload()])

    assert result.status == "persisted"
    assert result.deduplicated is False
    assert result.document_id is not None

    # the full native chain exists
    assert count(ingest_db, "procedure_order") == 1
    assert count(ingest_db, "procedure_order_code") == 1
    assert count(ingest_db, "procedure_report") == 1
    assert count(ingest_db, "procedure_result") == _N_ANALYTES

    # every result row links back to the source document
    with ingest_db.connect() as conn:
        rows = writer.read_rows(
            conn, "SELECT document_id, result_text, result, abnormal FROM procedure_result"
        )
        assert all(int(r["document_id"]) == result.document_id for r in rows)
        by_name = {r["result_text"]: r for r in rows}
        assert by_name["Glucose"]["result"] == "142"
        assert by_name["Glucose"]["abnormal"] == "yes"
        assert by_name["Sodium"]["abnormal"] == ""

        # provenance: one row per persisted analyte, with text-layer bboxes
        prov = writer.read_rows(conn, "SELECT * FROM copilot.provenance")
        assert len(prov) == _N_ANALYTES
        assert all(p["bbox_origin"] == "text_layer" for p in prov)
        assert all(p["target_table"] == "procedure_result" for p in prov)
        assert count(ingest_db, "copilot.ingestion_run") == 1

        refs = provenance.list_citations(conn, result.document_id)
        assert len(refs) == _N_ANALYTES

    # the result card carries BOTH the chart-row and document citations
    source_refs = [r for r in result.persisted if isinstance(r, SourceRef)]
    doc_refs = [r for r in result.persisted if isinstance(r, DocumentRef)]
    assert len(source_refs) == _N_ANALYTES
    assert len(doc_refs) == _N_ANALYTES
    assert all(r.table == "procedure_result" and r.primary_key > 0 for r in source_refs)
    assert all(r.bbox is not None and r.bbox.origin == "text_layer" for r in doc_refs)
    assert result.fields_extracted > 0
    assert result.fields_failed_verification == 0


def test_idempotent_rerun_zero_new_rows(ingest_db, ingest_settings):
    first = _run(ingest_db, [lab_payload()])
    assert first.status == "persisted"
    snapshot = {
        t: count(ingest_db, t)
        for t in (
            "documents", "procedure_order", "procedure_order_code", "procedure_report",
            "procedure_result", "copilot.ingestion_run", "copilot.provenance",
        )
    }

    second = _run(ingest_db, [lab_payload()], cid="cid-lab-2")
    assert second.status == "deduplicated"
    assert second.deduplicated is True
    assert second.document_id == first.document_id
    after = {t: count(ingest_db, t) for t in snapshot}
    assert after == snapshot  # zero new rows anywhere


def test_invented_value_not_persisted_surfaced_as_gap(ingest_db, ingest_settings):
    payload = lab_payload()
    # the VLM invents an analyte that is NOT in the document (quote absent from
    # the real text layer) — field_id slugs to 'troponin_i'
    payload["lab_values"].append(
        {
            "test_name": "Troponin I",
            "value": "12.3",
            "unit": "ng/mL",
            "reference_range": "",
            "abnormal_flag": "",
            "verbatim_quote": "Troponin I 12.3 ng/mL",
            "page": 1,
        }
    )
    result = _run(ingest_db, [payload])

    assert result.status == "persisted"  # the real fields still land
    assert count(ingest_db, "procedure_result") == _N_ANALYTES  # invented row absent
    with ingest_db.connect() as conn:
        rows = writer.read_rows(
            conn, "SELECT result_text FROM procedure_result WHERE result_text = 'Troponin I'"
        )
        assert rows == []
    assert any("troponin" in g and "failed_verification" in g for g in result.gaps)
    assert result.fields_failed_verification > 0


def test_value_not_inside_quote_fails_verification(ingest_db, ingest_settings):
    payload = lab_payload()
    glucose = payload["lab_values"][0]
    assert glucose["test_name"] == "Glucose"  # field_id slugs to 'glucose'
    # the claimed value is not inside the row's real verbatim quote
    glucose["value"] = "199"
    result = _run(ingest_db, [payload])

    assert count(ingest_db, "procedure_result") == _N_ANALYTES - 1  # glucose held back
    with ingest_db.connect() as conn:
        assert writer.read_rows(
            conn, "SELECT 1 FROM procedure_result WHERE result_text = 'Glucose'"
        ) == []
    assert any("glucose.value" in g and "failed_verification" in g for g in result.gaps)


def test_missing_reference_range_is_gap_while_row_persists(ingest_db, ingest_settings):
    payload = lab_payload()
    glucose = payload["lab_values"][0]
    glucose["reference_range"] = ""  # the model reports no range for this row
    result = _run(ingest_db, [payload])

    assert result.status == "persisted"
    assert count(ingest_db, "procedure_result") == _N_ANALYTES  # row still lands
    with ingest_db.connect() as conn:
        row = writer.read_rows(
            conn, "SELECT `range`, result FROM procedure_result WHERE result_text = 'Glucose'"
        )[0]
        assert row["result"] == "142"
        assert row["range"] == ""  # empty, never invented
    assert any(g.startswith("glucose.reference_range") and "not_found" in g
               for g in result.gaps)


def test_scanned_document_needs_review_nothing_persisted(ingest_db, ingest_settings):
    # a VLM that (plausibly) reads the scan perfectly — verification still
    # cannot corroborate any of it, because there is no text layer
    result = _run(ingest_db, [lab_payload()], pdf=LAB_SCANNED_PDF, cid="cid-scan")

    assert result.status == "needs_review"
    assert result.persisted == []
    assert len(result.needs_review) > 0
    for t in ("procedure_order", "procedure_order_code", "procedure_report",
              "procedure_result", "lists", "copilot.ingestion_run",
              "copilot.provenance", "copilot.intake_fact"):
        assert count(ingest_db, t) == 0, t
    assert "review" in (result.notes or "").lower()


def test_schema_invalid_after_retries_fails_with_zero_writes(ingest_db, ingest_settings):
    # every reply lacks a tool_use block -> ExtractionInvalid after the retry
    # budget -> fail closed, zero writes.
    result = _run(ingest_db, [None])

    assert result.status == "failed"
    assert result.persisted == []
    for t in ("procedure_order", "procedure_order_code", "procedure_report",
              "procedure_result", "copilot.ingestion_run", "copilot.provenance"):
        assert count(ingest_db, t) == 0, t


def test_mid_persist_error_rolls_back_everything(ingest_db, ingest_settings, monkeypatch):
    calls = {"n": 0}
    real = provenance.insert_provenance

    def explode(*a, **kw):
        calls["n"] += 1
        if calls["n"] >= 3:  # fail partway through the provenance loop
            raise RuntimeError("disk full")
        return real(*a, **kw)

    monkeypatch.setattr(provenance, "insert_provenance", explode)
    monkeypatch.setattr(service.provenance, "insert_provenance", explode)

    result = _run(ingest_db, [lab_payload()])

    assert result.status == "failed"
    assert calls["n"] >= 3
    # the WHOLE transaction rolled back: no partial chain, no run row
    for t in ("procedure_order", "procedure_order_code", "procedure_report",
              "procedure_result", "copilot.ingestion_run", "copilot.provenance"):
        assert count(ingest_db, t) == 0, t


def test_quote_verified_confidence_tiers(ingest_db, ingest_settings):
    payload = lab_payload()
    # force a normalized-only match for one row (extra internal whitespace in
    # the shared row quote)
    glucose = payload["lab_values"][0]
    exact_quote = lab_row_quote(GROUND_TRUTH["lab_simple.pdf"]["analytes"][0])
    glucose["verbatim_quote"] = exact_quote.replace(" 142 ", "  142  ")
    result = _run(ingest_db, [payload])
    assert result.status == "persisted"
    with ingest_db.connect() as conn:
        prov = {p["field_id"]: p for p in writer.read_rows(
            conn, "SELECT field_id, confidence FROM copilot.provenance")}
    assert prov["glucose"]["confidence"] == pytest.approx(0.7)  # normalized tier
    assert prov["sodium"]["confidence"] >= 0.9  # exact tier
