"""HIPAA invariant for the ingestion pipeline: a FULL ingest cycle emits no
PHI to the log sink — no patient names, no clinical values, no quotes, and
critically NO FILENAMES (uploaded filenames routinely contain patient names;
`filename` is deliberately absent from the correlation.py allowlist).

Failure mode guarded: any new log line in the ingest path (including error
paths) accidentally carrying a value or the upload filename. The allowlist is
the enforcement; this test proves the pipeline end-to-end against the real
fixture content.
"""

from __future__ import annotations

import json
import logging

from app.ingest import service

from _helpers import LAB_SIMPLE_PDF, MEDLIST_SIMPLE_PDF, StubVLM, lab_payload, medlist_payload

# strings that MUST NOT appear anywhere in captured log output
_FORBIDDEN = [
    "TESTPATIENT",            # patient name (fixture)
    "ALPHA",
    "Glucose",                # analyte names / values / units / ranges
    "mg/dL",
    "70-99",
    "742-118-4460-0",         # specimen id
    "CHEN",                   # provider name
    "lab_simple",             # the uploaded FILENAME (never logged)
    "upload.pdf",
    "Hemoglobin",
]


def test_full_ingest_emits_no_phi_in_logs(ingest_db, ingest_settings, caplog):
    with caplog.at_level(logging.INFO):
        result = service.attach_and_extract(
            pid=101,
            file_bytes=LAB_SIMPLE_PDF,
            filename="lab_simple TESTPATIENT ALPHA.pdf",  # worst-case filename
            mimetype="application/pdf",
            doc_type="lab_report",
            correlation_id="cid-phi-1",
            client=StubVLM([lab_payload()]),
        )
    assert result.status == "persisted"
    assert len(caplog.records) > 0  # the pipeline DID log (events exist)

    for rec in caplog.records:
        fields = getattr(rec, "fields", None) or {}
        line = rec.getMessage() + " " + json.dumps(fields, default=str)
        for needle in _FORBIDDEN:
            assert needle not in line, f"PHI {needle!r} leaked in log line: {line}"
        # the filename key itself must never be logged
        assert "filename" not in fields
        # quotes must never be logged
        assert "quote" not in fields
        assert "verbatim_quote" not in fields


# Medication-list run: drug names, strengths, frequencies, the prescriber and
# the patient name must never reach the sink (field ids are lowercase slugs;
# the capitalized printed forms below are the PHI shapes).
_MEDLIST_FORBIDDEN = [
    "TESTPATIENT",
    "ALPHA",
    "Metformin",
    "Lisinopril",
    "Atorvastatin",
    "Sertraline",
    "1000 mg",
    "BID",
    "Chen",
    "medlist_simple",  # the uploaded FILENAME (never logged)
]


def test_medlist_ingest_emits_no_phi_in_logs(ingest_db, ingest_settings, caplog):
    with caplog.at_level(logging.INFO):
        result = service.attach_and_extract(
            pid=101,
            file_bytes=MEDLIST_SIMPLE_PDF,
            filename="medlist_simple TESTPATIENT ALPHA.pdf",  # worst-case filename
            mimetype="application/pdf",
            doc_type="medication_list",
            correlation_id="cid-phi-medlist-1",
            client=StubVLM([medlist_payload()]),
        )
    assert result.status == "persisted"
    assert len(caplog.records) > 0

    for rec in caplog.records:
        fields = getattr(rec, "fields", None) or {}
        line = rec.getMessage() + " " + json.dumps(fields, default=str)
        for needle in _MEDLIST_FORBIDDEN:
            assert needle not in line, f"PHI {needle!r} leaked in log line: {line}"
        assert "filename" not in fields
        assert "quote" not in fields
        assert "verbatim_quote" not in fields
