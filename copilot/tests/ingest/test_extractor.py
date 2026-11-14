"""extractor: the forced-tool schema boundary (D3.1/D3.2).

Failure modes guarded:
- `document_id` leaking into the VLM schema would let the model claim
  attachment to an arbitrary document (invariant: service-assigned only);
- unbounded/absent retry would either loop forever or let schema-invalid
  output propagate (retry budget = settings.extract_max_retries, then a
  typed, explicit failure);
- mock mode hitting the network would break hermetic tests and load tests.
"""

from __future__ import annotations

import pytest

from app.ingest import extractor
from app.ingest.extractor import ExtractionInvalid, tool_schema
from app.ingest.schemas import (
    IntakeFormExtraction,
    LabReportExtraction,
    MedicationListExtraction,
)

from _helpers import LAB_SIMPLE_PDF, MEDLIST_SIMPLE_PDF, StubVLM, lab_payload, medlist_payload


def test_tool_schema_excludes_document_id():
    # The FLAT forced-tool schema never exposes document_id or a bbox — the
    # model supplies neither (service assigns the id; verification supplies bbox).
    for doc_type in ("lab_report", "intake_form", "medication_list"):
        schema = tool_schema(doc_type)
        assert "document_id" not in schema.get("properties", {})
        assert "document_id" not in (schema.get("required") or [])
        assert "bbox" not in schema.get("properties", {})
    # the Pydantic model itself still HAS the field (service-assigned)
    assert "document_id" in LabReportExtraction.model_fields


def test_mock_mode_returns_canned_extraction(ingest_settings, monkeypatch):
    monkeypatch.setattr(ingest_settings, "mock_llm", True)
    out = extractor.extract(LAB_SIMPLE_PDF, "application/pdf", "lab_report")
    assert isinstance(out, LabReportExtraction)
    assert out.document_id is None
    assert out.patient_name.status == "not_found"  # canned = honest gaps, no invention
    assert out.lab_values == []


def test_retry_on_invalid_then_success_exactly_two_calls(ingest_settings):
    # `None` → a response with no tool_use block (the retry-on-invalid trigger).
    stub = StubVLM([None, lab_payload()])
    out = extractor.extract(
        LAB_SIMPLE_PDF, "application/pdf", "lab_report",
        client=stub, settings=ingest_settings,
    )
    assert stub.calls == 2
    assert isinstance(out, LabReportExtraction)
    assert len(out.lab_values) == 10


def test_all_retries_invalid_raises_typed_error(ingest_settings):
    stub = StubVLM([None])  # every reply lacks a tool_use block
    with pytest.raises(ExtractionInvalid) as exc:
        extractor.extract(
            LAB_SIMPLE_PDF, "application/pdf", "lab_report",
            client=stub, settings=ingest_settings,
        )
    # 1 initial call + extract_max_retries(2) re-prompts, then fail closed
    assert stub.calls == 3
    assert exc.value.retries == 2


def test_model_supplied_document_id_is_stripped(ingest_settings):
    payload = lab_payload()
    payload["document_id"] = 999_999  # the model tries to claim a document
    stub = StubVLM([payload])
    out = extractor.extract(
        LAB_SIMPLE_PDF, "application/pdf", "lab_report",
        client=stub, settings=ingest_settings,
    )
    assert out.document_id is None  # only the service assigns it


# ---- medication_list (third doc type, T4) --------------------------------------


def test_medlist_tool_schema_required_row_fields():
    """FAILURE MODE GUARDED: a row schema that lets the model omit the quote or
    page would produce unverifiable rows (nothing to check against the text
    layer) — name/verbatim_quote/page must stay required."""
    schema = tool_schema("medication_list")
    assert set(schema["required"]) == {"medications"}
    row = schema["properties"]["medications"]["items"]
    assert set(row["required"]) == {"name", "verbatim_quote", "page"}
    for sub in ("name", "strength", "route", "frequency", "status", "prescriber"):
        assert sub in row["properties"]


def test_medlist_mock_mode_returns_canned_extraction(ingest_settings, monkeypatch):
    monkeypatch.setattr(ingest_settings, "mock_llm", True)
    out = extractor.extract(MEDLIST_SIMPLE_PDF, "application/pdf", "medication_list")
    assert isinstance(out, MedicationListExtraction)
    assert out.document_id is None
    assert out.medications == []  # canned = honest gaps, no invention


def test_medlist_mapping_shares_row_quote_and_slug_ids(ingest_settings):
    """FAILURE MODE GUARDED: subfields drifting away from the row's quote/page
    would let a value be 'verified' against a different line than the one the
    model claims to have read."""
    payload = medlist_payload()
    stub = StubVLM([payload])
    out = extractor.extract(
        MEDLIST_SIMPLE_PDF, "application/pdf", "medication_list",
        client=stub, settings=ingest_settings,
    )
    assert isinstance(out, MedicationListExtraction)
    assert [m.field_id for m in out.medications] == [
        "metformin", "lisinopril", "atorvastatin", "sertraline",
    ]
    met = out.medications[0]
    quote = "Metformin 1000 mg oral BID Active Chen"
    for sub in ("name", "strength", "route", "frequency", "status", "prescriber"):
        env = getattr(met, sub)
        assert env.status == "extracted"
        assert env.verbatim_quote == quote  # every subfield rides the ROW quote
        assert env.page == 1
    assert met.frequency.value == "BID"  # abbreviation preserved, never expanded
    # a missing subfield is a gap, never an empty invented value
    payload2 = medlist_payload()
    payload2["medications"][0]["frequency"] = ""
    out2 = extractor.extract(
        MEDLIST_SIMPLE_PDF, "application/pdf", "medication_list",
        client=StubVLM([payload2]), settings=ingest_settings,
    )
    assert out2.medications[0].frequency.status == "not_found"


def test_medlist_duplicate_names_get_collision_suffix(ingest_settings):
    """FAILURE MODE GUARDED: two rows slugging to the same field_id would
    collide on UNIQUE(document_id, field_id) in copilot.provenance and roll the
    whole ingest back — duplicates must get a deterministic suffix."""
    payload = medlist_payload()
    dup = dict(payload["medications"][0])  # a second Metformin row
    payload["medications"].append(dup)
    out = extractor.extract(
        MEDLIST_SIMPLE_PDF, "application/pdf", "medication_list",
        client=StubVLM([payload]), settings=ingest_settings,
    )
    ids = [m.field_id for m in out.medications]
    assert len(ids) == len(set(ids))
    assert ids[0] == "metformin" and ids[-1] == "metformin_2"


def test_medlist_prompt_carries_no_normalization_hint(ingest_settings):
    """FAILURE MODE GUARDED: a model 'helpfully' expanding BID -> 'twice daily'
    fails verbatim verification on every abbreviated row; the med-list user
    message must carry the copy-exactly instruction (and only that doc type)."""
    stub = StubVLM([medlist_payload()])
    extractor.extract(
        MEDLIST_SIMPLE_PDF, "application/pdf", "medication_list",
        client=stub, settings=ingest_settings,
    )
    text_blocks = [
        b["text"]
        for m in stub.requests[-1]["messages"]
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert any("BID stays BID" in t for t in text_blocks)

    lab_stub = StubVLM([lab_payload()])
    extractor.extract(
        LAB_SIMPLE_PDF, "application/pdf", "lab_report",
        client=lab_stub, settings=ingest_settings,
    )
    lab_texts = [
        b["text"]
        for m in lab_stub.requests[-1]["messages"]
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert not any("BID stays BID" in t for t in lab_texts)


def test_model_cannot_inject_a_bbox(ingest_settings):
    # The flat schema has no bbox field and the mapper reads only value/quote/
    # page, so a model that smuggles a bbox into its output is ignored — bbox is
    # granted EXCLUSIVELY by deterministic text-layer verification downstream.
    payload = lab_payload()
    payload["patient_name"]["bbox"] = {
        "page": 1, "x0": 0.1, "y0": 0.1, "x1": 0.3, "y1": 0.12, "origin": "text_layer",
    }
    stub = StubVLM([payload])
    out = extractor.extract(
        LAB_SIMPLE_PDF, "application/pdf", "lab_report",
        client=stub, settings=ingest_settings,
    )
    assert out.patient_name.bbox is None  # model-supplied bbox never survives
