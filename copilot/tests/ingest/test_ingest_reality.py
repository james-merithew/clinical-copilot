"""MariaDB-reality regressions the hermetic single-patient suite missed (W2).

Each test guards a defect that passed SQLite/single-patient tests but breaks on
real MariaDB or across patients:

- collection dates are persisted into `datetime` columns, so a document-format
  date ('07/10/2026') must be normalized to canonical `YYYY-MM-DD` before
  persist — never injected raw (strict MariaDB 1292-errors; non-strict zeroes it);
- the REST-primary document-id recovery re-SELECT must run on a FRESH connection,
  not the writer's open REPEATABLE READ transaction (the just-committed row is
  invisible to that snapshot);
- dedup/recovery is per-(pid, hash): the same bytes uploaded for a second patient
  get their OWN documents row + provenance, never a cross-patient reuse that
  collides on UNIQUE(document_id, field_id).
"""

from __future__ import annotations

import json

import pytest

from app.ingest import service, writer
from app.ingest.schemas import ExtractedField, ExtractedLabValue, LabReportExtraction

from _helpers import GROUND_TRUTH, LAB_SIMPLE_PDF, StubVLM, lab_payload

_N_ANALYTES = len(GROUND_TRUTH["lab_simple.pdf"]["analytes"])  # 10


# --- Finding 1: collection-date normalization ---------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-07-10", "2026-07-10"),  # ISO, already canonical
        ("2026/07/10", "2026-07-10"),
        ("07/10/2026", "2026-07-10"),  # US MM/DD/YYYY
        ("07-10-2026", "2026-07-10"),
        ("10-Jul-2026", "2026-07-10"),
        ("Jul 10, 2026", "2026-07-10"),
        ("July 10, 2026", "2026-07-10"),
        ("10 July 2026", "2026-07-10"),
        ("2026-07-10 08:30:00", "2026-07-10"),  # trailing time tolerated
        ("07/10/26", "2026-07-10"),  # 2-digit year
        ("", None),
        (None, None),
        ("not a date", None),
        ("13/45/2026", None),  # impossible calendar date -> unparseable
    ],
)
def test_normalize_collection_date(raw, expected):
    assert writer.normalize_collection_date(raw) == expected


def _lab_extraction(coll_value: str, coll_status: str = "extracted") -> LabReportExtraction:
    q = "Glucose 142 mg/dL 70-99"
    lv = ExtractedLabValue(
        field_id="cmp_glucose",
        name=ExtractedField[str](value="Glucose", page=1, verbatim_quote=q, status="extracted"),
        value=ExtractedField[str](value="142", page=1, verbatim_quote=q, status="extracted"),
    )
    return LabReportExtraction(
        collection_date=ExtractedField[str](
            value=coll_value, page=1, verbatim_quote="Date Collected: " + str(coll_value),
            status=coll_status,
        ),
        lab_values=[lv],
        document_id=1001,
    )


def test_non_iso_collection_date_persisted_canonical(ingest_db):
    """A US-format printed date must reach every datetime column as YYYY-MM-DD."""
    ext = _lab_extraction("07/10/2026")
    with ingest_db.begin() as conn:
        facts = writer.persist_lab_chain(conn, 101, ext, 1001, "cid-date", "pv")
    assert facts  # the analyte persisted

    with ingest_db.connect() as conn:
        po = writer.read_rows(
            conn, "SELECT date_ordered, date_collected FROM procedure_order"
        )[0]
        assert po["date_ordered"] == "2026-07-10"
        assert po["date_collected"] == "2026-07-10"
        rep = writer.read_rows(conn, "SELECT date_collected FROM procedure_report")[0]
        assert rep["date_collected"] == "2026-07-10"
        res = writer.read_rows(conn, "SELECT date FROM procedure_result")[0]
        assert res["date"] == "2026-07-10"


def test_unparseable_collection_date_is_null_and_raw_kept(ingest_db):
    """An unparseable date becomes NULL (never injected) and the raw value is
    preserved in report_notes for audit."""
    ext = _lab_extraction("Received Tuesday")
    with ingest_db.begin() as conn:
        writer.persist_lab_chain(conn, 101, ext, 1001, "cid-bad-date", "pv")

    with ingest_db.connect() as conn:
        po = writer.read_rows(
            conn, "SELECT date_ordered, date_collected FROM procedure_order"
        )[0]
        assert po["date_collected"] is None  # NULL, never the raw string
        assert po["date_ordered"] != "Received Tuesday"  # falls back to timestamp
        rep = writer.read_rows(conn, "SELECT report_notes FROM procedure_report")[0]
        notes = json.loads(rep["report_notes"])
        assert notes["collection_date_raw"] == "Received Tuesday"
        assert notes["collection_date_parsed"] is None


# --- Finding 2: REST id-recovery must not reuse the writer's open txn ----------


def test_rest_recovery_read_uses_fresh_connection(ingest_db, ingest_settings, monkeypatch):
    """The post-upload id-recovery SELECT must run on a connection distinct from
    the writer's open store transaction (which holds a stale REPEATABLE READ
    snapshot on MariaDB)."""
    seen_conns: list[object] = []
    real = writer.find_document_id_by_hash

    def spy(*args, **kwargs):
        seen_conns.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(writer, "find_document_id_by_hash", spy)
    monkeypatch.setattr(service.writer, "find_document_id_by_hash", spy)

    class _Emr:
        """OpenEMR stand-in: the upload 'succeeds' but (as the real endpoint
        does) returns no id — recovery is by hash on a fresh connection."""

        def upload_document(self, pid, file_bytes, filename, mimetype, doc_type):
            return True

    result = service.attach_and_extract(
        pid=101,
        file_bytes=LAB_SIMPLE_PDF,
        filename="lab.pdf",
        mimetype="application/pdf",
        doc_type="lab_report",
        correlation_id="cid-rest",
        client=StubVLM([lab_payload()]),
        emr=_Emr(),
    )

    # The ingest still completes (recovery-miss falls through to the flag-gated
    # fallback in this hermetic harness), but the point is structural:
    assert result.status == "persisted"
    # first call = pre-REST reuse lookup on the writer's store txn conn;
    # last call  = post-REST recovery, which must be a DIFFERENT connection.
    assert len(seen_conns) >= 2
    assert seen_conns[-1] is not seen_conns[0]


# --- Finding 3: per-patient document dedup ------------------------------------


def _ingest(engine, pid: int, cid: str):
    return service.attach_and_extract(
        pid=pid,
        file_bytes=LAB_SIMPLE_PDF,
        filename="lab.pdf",
        mimetype="application/pdf",
        doc_type="lab_report",
        correlation_id=cid,
        client=StubVLM([lab_payload()]),
    )


def test_same_bytes_two_patients_get_two_documents(ingest_db, ingest_settings):
    r1 = _ingest(ingest_db, 101, "cid-a")
    assert r1.status == "persisted"

    r2 = _ingest(ingest_db, 102, "cid-b")
    assert r2.status == "persisted"  # NOT a UNIQUE-constraint hard-fail
    assert r2.document_id != r1.document_id

    with ingest_db.connect() as conn:
        docs = writer.read_rows(conn, "SELECT id, foreign_id FROM documents ORDER BY id")
        assert len(docs) == 2
        by_pid = {int(d["foreign_id"]): int(d["id"]) for d in docs}
        assert set(by_pid) == {101, 102}
        doc101, doc102 = by_pid[101], by_pid[102]

        # two independent provenance sets, each keyed to its own document
        prov = writer.read_rows(
            conn, "SELECT document_id, field_id FROM copilot.provenance"
        )
        assert len(prov) == 2 * _N_ANALYTES
        p101 = {p["field_id"] for p in prov if int(p["document_id"]) == doc101}
        p102 = {p["field_id"] for p in prov if int(p["document_id"]) == doc102}
        assert len(p101) == _N_ANALYTES
        assert len(p102) == _N_ANALYTES

        # no cross-patient link: each chain cites only its own patient's document
        res = writer.read_rows(conn, "SELECT document_id FROM procedure_result")
        assert {int(r["document_id"]) for r in res} == {doc101, doc102}
