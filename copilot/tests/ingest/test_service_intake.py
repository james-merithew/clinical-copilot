"""attach_and_extract, intake path — the D2 intake policy.

Failure modes guarded:
- meds/allergies not landing in `lists` (the table OpenEMR itself reads)
  would make the intake invisible to the chart;
- demographics silently OVERWRITING patient_data would corrupt the system of
  record — the policy is compare-and-flag only (mismatch -> intake_fact);
- chief concern / family history must land in the sidecar-owned intake_fact,
  not be fabricated into clinical notes.
"""

from __future__ import annotations

from app.ingest import service, writer

from _helpers import INTAKE_SIMPLE_PDF, StubVLM, count, intake_payload


def test_intake_lists_facts_and_demographic_flags(ingest_db, ingest_settings):
    # intake_simple.pdf is TESTPATIENT/1970 — deliberately NOT pid 101's chart
    # identity (Delgado/1961) so the compare-and-flag path must fire.
    result = service.attach_and_extract(
        pid=101,
        file_bytes=INTAKE_SIMPLE_PDF,
        filename="intake_simple.pdf",
        mimetype="application/pdf",
        doc_type="intake_form",
        correlation_id="cid-intake-1",
        client=StubVLM([intake_payload()]),
    )
    assert result.status == "persisted"

    with ingest_db.connect() as conn:
        meds = writer.read_rows(
            conn, "SELECT title, comments, type FROM lists WHERE type = 'medication'"
        )
        allergies = writer.read_rows(
            conn, "SELECT title, reaction, type FROM lists WHERE type = 'allergy'"
        )
        facts = writer.read_rows(
            conn, "SELECT kind, field_id, value, chart_value FROM copilot.intake_fact"
        )

    assert {m["title"] for m in meds} == {"Metformin", "Lisinopril", "Atorvastatin"}
    assert any("1000 mg" in (m["comments"] or "") for m in meds)
    assert {a["title"] for a in allergies} == {"Penicillin", "Sulfa drugs"}
    assert any(a["reaction"] == "hives" for a in allergies)

    by_kind = {}
    for f in facts:
        by_kind.setdefault(f["kind"], []).append(f)
    assert [f["value"] for f in by_kind["chief_concern"]] == ["increased thirst and fatigue"]
    assert "family_history" in by_kind

    # compare-and-flag: name + DOB mismatch flagged, chart value preserved
    mism = {f["field_id"]: f for f in by_kind["demographic_mismatch"]}
    assert set(mism) == {"patient_name", "date_of_birth"}
    assert "Delgado" in mism["patient_name"]["chart_value"]
    assert mism["date_of_birth"]["chart_value"] == "1961-04-12"

    # THE invariant: patient_data was not touched
    with ingest_db.connect() as conn:
        chart = writer.read_rows(
            conn, "SELECT fname, lname, DOB FROM patient_data WHERE pid = 101"
        )[0]
    assert (chart["fname"], chart["lname"], chart["DOB"]) == (
        "Marcus", "Delgado", "1961-04-12",
    )

    # dedup applies to intake too
    again = service.attach_and_extract(
        pid=101,
        file_bytes=INTAKE_SIMPLE_PDF,
        filename="intake_simple.pdf",
        mimetype="application/pdf",
        doc_type="intake_form",
        correlation_id="cid-intake-2",
        client=StubVLM([intake_payload()]),
    )
    assert again.status == "deduplicated"
    assert count(ingest_db, "lists") == 5
