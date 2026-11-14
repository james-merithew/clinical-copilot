"""attach_and_extract, medication-list path — the third document type (T4).

The core decision under test: a document-reported med list persists to
`lists` (type='medication'), the SAME surface as intake current-meds, and
NEVER to `prescriptions` — a document must not gain prescriber authority
(`tools/medications.py` treats `prescriptions` as the ordered-authority row;
AUDIT.md divergence rule).

Failure modes guarded:
- med rows landing anywhere but `lists` type='medication' (wrong authority
  surface, or invisible to the chart);
- a Discontinued row persisting as active (activity must be 0 for
  discontinued/stopped/held/inactive statuses);
- a misread strength reaching `comments` after failing verification;
- a missing frequency silently absent instead of surfaced as a gap while the
  verified row still persists (graceful degradation, not all-or-nothing);
- re-upload duplicating the list (UNIQUE(content_hash, pid) idempotency).
"""

from __future__ import annotations

from app.contracts import DocumentRef, SourceRef
from app.ingest import service, writer

from _helpers import (
    FIXTURES,
    MEDLIST_SIMPLE_PDF,
    StubVLM,
    count,
    medlist_payload,
)

MEDLIST_DELGADO_PDF = (FIXTURES / "demo_medlist_delgado.pdf").read_bytes()


def _run(payloads, pdf=MEDLIST_SIMPLE_PDF, fixture="medlist_simple.pdf",
         pid=101, cid="cid-medlist-1"):
    return service.attach_and_extract(
        pid=pid,
        file_bytes=pdf,
        filename=fixture,
        mimetype="application/pdf",
        doc_type="medication_list",
        correlation_id=cid,
        client=StubVLM(payloads),
    )


def test_happy_path_persists_to_lists_never_prescriptions(ingest_db, ingest_settings):
    result = _run(
        [medlist_payload("demo_medlist_delgado.pdf")],
        pdf=MEDLIST_DELGADO_PDF,
        fixture="demo_medlist_delgado.pdf",
    )

    assert result.status == "persisted"
    assert result.doc_type == "medication_list"
    assert result.document_id is not None

    with ingest_db.connect() as conn:
        meds = writer.read_rows(
            conn,
            "SELECT title, comments, activity, begdate, type, user "
            "FROM lists WHERE type = 'medication'",
        )
        prov = writer.read_rows(conn, "SELECT * FROM copilot.provenance")

    by_title = {m["title"]: m for m in meds}
    assert set(by_title) == {"Metformin", "Lisinopril", "Atorvastatin", "Glipizide", "Aspirin"}
    # comments = verified-only strength/route/frequency/prescriber, as printed
    assert by_title["Metformin"]["comments"] == "1000 mg oral twice daily Chen"
    # the missing-frequency row persists WITHOUT a frequency (gap, not invention)
    assert by_title["Aspirin"]["comments"] == "81 mg oral Chen"
    # Discontinued -> activity=0; everything else active
    assert by_title["Glipizide"]["activity"] == 0
    assert all(by_title[t]["activity"] == 1 for t in by_title if t != "Glipizide")
    # begdate from the VERIFIED list date (normalized), not "now"
    assert all(str(m["begdate"]).startswith("2026-07-12") for m in meds)
    assert all(m["user"] == "copilot_ingest" for m in meds)

    # the write perimeter: lists only — no lab chain, no prescriptions table at all
    assert count(ingest_db, "procedure_order") == 0
    assert count(ingest_db, "procedure_result") == 0
    assert count(ingest_db, "copilot.intake_fact") == 0

    # provenance: one row per persisted med, text-layer authority, page+quote
    assert len(prov) == 5
    assert all(p["target_table"] == "lists" for p in prov)
    assert all(p["bbox_origin"] == "text_layer" for p in prov)
    assert all(p["page"] == 1 and p["quote"] for p in prov)
    fids = {p["field_id"] for p in prov}
    assert fids == {"metformin", "lisinopril", "atorvastatin", "glipizide", "aspirin"}

    # the result card pairs a chart-row SourceRef with a DocumentRef per med
    source_refs = [r for r in result.persisted if isinstance(r, SourceRef)]
    doc_refs = [r for r in result.persisted if isinstance(r, DocumentRef)]
    assert len(source_refs) == 5 and len(doc_refs) == 5
    assert all(r.table == "lists" and r.primary_key > 0 for r in source_refs)
    assert all(r.bbox is not None and r.bbox.origin == "text_layer" for r in doc_refs)
    assert all(r.page == 1 and r.quote for r in doc_refs)

    # the frequency gap is stated in words
    assert any(g.startswith("aspirin.frequency") and "not_found" in g for g in result.gaps)


def test_dedup_second_upload_zero_new_rows(ingest_db, ingest_settings):
    first = _run([medlist_payload("demo_medlist_delgado.pdf")],
                 pdf=MEDLIST_DELGADO_PDF, fixture="demo_medlist_delgado.pdf")
    assert first.status == "persisted"
    snapshot = {
        t: count(ingest_db, t)
        for t in ("documents", "lists", "copilot.ingestion_run", "copilot.provenance")
    }

    second = _run([medlist_payload("demo_medlist_delgado.pdf")],
                  pdf=MEDLIST_DELGADO_PDF, fixture="demo_medlist_delgado.pdf",
                  cid="cid-medlist-2")
    assert second.status == "deduplicated"
    assert second.deduplicated is True
    assert second.document_id == first.document_id
    assert {t: count(ingest_db, t) for t in snapshot} == snapshot


def test_missing_frequency_is_gap_while_row_persists(ingest_db, ingest_settings):
    payload = medlist_payload()
    met = payload["medications"][0]
    assert met["name"] == "Metformin"
    met["frequency"] = ""  # the model reports no frequency for this row

    result = _run([payload])

    assert result.status == "persisted"
    with ingest_db.connect() as conn:
        row = writer.read_rows(
            conn, "SELECT comments FROM lists WHERE title = 'Metformin'"
        )[0]
    assert row["comments"] == "1000 mg oral Chen"  # frequency absent, never invented
    assert any(g.startswith("metformin.frequency") and "not_found" in g
               for g in result.gaps)


def test_misread_strength_fails_verification_and_stays_out_of_comments(
    ingest_db, ingest_settings
):
    payload = medlist_payload()
    met = payload["medications"][0]
    met["strength"] = "100 mg"  # misread; the quote still says 1000 mg

    result = _run([payload])

    assert result.status == "persisted"  # the verified subfields still land
    with ingest_db.connect() as conn:
        row = writer.read_rows(
            conn, "SELECT comments FROM lists WHERE title = 'Metformin'"
        )[0]
    assert row["comments"] == "oral BID Chen"  # the failed strength is NOT there
    assert "100 mg" not in (row["comments"] or "")
    assert result.fields_failed_verification >= 1
    assert any(g.startswith("metformin.strength") and "failed_verification" in g
               for g in result.gaps)
    assert "failed text-layer verification" in (result.notes or "")


def test_unverified_name_row_not_persisted(ingest_db, ingest_settings):
    payload = medlist_payload()
    payload["medications"].append(
        {
            "name": "Ghostodone",  # invented row: quote absent from the text layer
            "strength": "50 mg",
            "route": "oral",
            "frequency": "BID",
            "status": "Active",
            "prescriber": "Chen",
            "verbatim_quote": "Ghostodone 50 mg oral BID Active Chen",
            "page": 1,
        }
    )
    result = _run([payload])

    assert result.status == "persisted"  # the four real rows land
    assert count(ingest_db, "lists") == 4
    with ingest_db.connect() as conn:
        assert writer.read_rows(
            conn, "SELECT 1 FROM lists WHERE title = 'Ghostodone'"
        ) == []
    assert any("ghostodone" in g and "failed_verification" in g for g in result.gaps)
