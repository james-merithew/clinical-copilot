"""Week 2 contract tests — the citation union, graph records, and ingestion
envelopes (Step 0). Every case guards a named failure mode:

- discriminated-union round-trip → a ref deserializing as the WRONG class would
  silently drop page/bbox/quote (the Pydantic subclass-serialization trap D1
  exists to avoid).
- Week-1 wire compat → additive-only migration: old payloads must still
  validate, and old serializations must change by NOTHING except the new
  `source_type` discriminator tag.
- ExtractedField/BBox bounds → a malformed VLM envelope must be rejected at the
  schema boundary, never propagate into verification/persistence.
- evidence_items() → the worker seam must only ever emit extracted-status
  fields (gaps stay gaps, invariant #9).

Hermetic: no DB, no LLM.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.contracts import (
    AgentResponse,
    BBox,
    Claim,
    DocumentRef,
    EvidenceChunk,
    GuidelineRef,
    GuidelineSearchResult,
    Handoff,
    SourceRef,
    WorkerResult,
)
from app.ingest.schemas import (
    PROMPT_VERSION,
    ExtractedField,
    ExtractedLabValue,
    IngestResult,
    IntakeFormExtraction,
    IntakeListItem,
    LabReportExtraction,
    MedicationListExtraction,
    MedicationListItem,
)

# Captured from the codebase BEFORE the Week 2 change (Week 1 wire shape).
_WEEK1_CLAIM_JSON = (
    '{"text":"Metformin 1000 mg is active","source_refs":'
    '[{"table":"prescriptions","primary_key":147,"label":"Rx Metformin"}]}'
)
_WEEK1_AGENT_RESPONSE_JSON = (
    '{"correlation_id":"cid-1","briefing":[' + _WEEK1_CLAIM_JSON + '],'
    '"constraint_flags":[],"action_chips":[],"quarantined_claims":[],'
    '"quarantined_count":0,"degraded":false,"notes":null}'
)


def _bbox(**over) -> BBox:
    base = dict(page=1, x0=0.1, y0=0.2, x1=0.5, y1=0.25, origin="text_layer")
    base.update(over)
    return BBox(**base)


# ---- Discriminated union round-trip ------------------------------------------


def test_union_round_trip_each_ref_kind():
    """FAILURE MODE GUARDED: a ref serializing without its tag, or deserializing
    into the wrong class (dropping page/bbox/quote), would break click-to-source."""
    claim = Claim(
        text="A1c 8.2 % exceeds the ADA target",
        source_refs=[
            SourceRef(table="procedure_result", primary_key=500, label="A1c 2026-05-01"),
            GuidelineRef(doc_id="ada-soc", chunk_id="ada-soc#glycemic-targets:2",
                         section="Glycemic targets", label="ADA SoC"),
            DocumentRef(document_id=42, page=1, field_id="a1c", bbox=_bbox(),
                        quote="Hemoglobin A1c 8.2 %", label="Lab report p.1 — A1c"),
        ],
    )
    wire = claim.model_dump_json()
    back = Claim.model_validate_json(wire)

    kinds = [type(r) for r in back.source_refs]
    assert kinds == [SourceRef, GuidelineRef, DocumentRef]
    tags = [json.loads(wire)["source_refs"][i]["source_type"] for i in range(3)]
    assert tags == ["patient_record", "guideline", "document"]
    # The document ref kept its region data through the round trip.
    doc = back.source_refs[2]
    assert doc.bbox is not None and doc.bbox.origin == "text_layer"
    assert doc.quote == "Hemoglobin A1c 8.2 %"
    # .key() is well-defined on every variant.
    assert back.source_refs[0].key() == ("procedure_result", 500)
    assert back.source_refs[1].key() == ("guideline", "ada-soc#glycemic-targets:2")
    assert back.source_refs[2].key() == ("documents", "42:a1c")


# ---- Week-1 wire compatibility -----------------------------------------------


def test_week1_claim_payload_without_tag_still_validates():
    """FAILURE MODE GUARDED: a strict discriminated union rejects tag-less
    input — Week 1 payloads (replayed transcripts, stored fixtures) carry no
    `source_type` and must keep validating as patient_record."""
    claim = Claim.model_validate_json(_WEEK1_CLAIM_JSON)
    ref = claim.source_refs[0]
    assert isinstance(ref, SourceRef)
    assert ref.source_type == "patient_record"
    assert ref.key() == ("prescriptions", 147)


def test_week1_claim_serialization_golden():
    """FAILURE MODE GUARDED: contract churn breaking Week 1 consumers. A claim
    built the old way must serialize identically to the committed Week 1 golden
    string EXCEPT for exactly one addition: the `source_type` discriminator tag
    (which must serialize — it is the machine-readable tag the assignment's
    citation shape and the client's switch depend on)."""
    claim = Claim(
        text="Metformin 1000 mg is active",
        source_refs=[SourceRef(table="prescriptions", primary_key=147, label="Rx Metformin")],
    )
    dumped = json.loads(claim.model_dump_json())
    for ref in dumped["source_refs"]:
        assert ref.pop("source_type") == "patient_record"
    assert json.dumps(dumped, separators=(",", ":")) == _WEEK1_CLAIM_JSON


def test_week1_agent_response_payload_still_validates_with_defaults():
    """FAILURE MODE GUARDED: `AgentResponse` gained fields; an old payload
    without them must validate and the new fields must default empty."""
    resp = AgentResponse.model_validate_json(_WEEK1_AGENT_RESPONSE_JSON)
    assert resp.guideline_evidence == []
    assert resp.handoffs == []
    assert resp.briefing[0].source_refs[0].primary_key == 147


# ---- BBox bounds ---------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"page": 0},          # pages are 1-based
        {"x0": -0.1},         # below normalized range
        {"x1": 1.2},          # above normalized range
        {"y1": 400.0},        # pixel coords passed where normalized required
        {"origin": "guess"},  # unknown coordinate authority
    ],
)
def test_bbox_bounds_rejection(over):
    """FAILURE MODE GUARDED: a VLM emitting pixel coordinates or nonsense pages
    must be rejected at the schema boundary, not rendered as a bogus overlay."""
    with pytest.raises(ValidationError):
        _bbox(**over)


# ---- ExtractedField envelope ---------------------------------------------------


def test_extracted_field_status_and_bounds():
    """FAILURE MODE GUARDED: an 'extracted' field with no value is an invented
    fact slot; confidence/page outside bounds is a malformed envelope."""
    ok = ExtractedField[str](value="8.2", page=1, confidence=0.97,
                             verbatim_quote="Hemoglobin A1c 8.2 %", status="extracted")
    assert ok.status == "extracted"

    with pytest.raises(ValidationError):
        ExtractedField[str](status="extracted")  # extracted requires a value
    with pytest.raises(ValidationError):
        ExtractedField[str](value="x", confidence=1.5, status="extracted")
    with pytest.raises(ValidationError):
        ExtractedField[str](value="x", page=0, status="extracted")
    with pytest.raises(ValidationError):
        ExtractedField[str](value="x", status="hallucinated")

    # A gap is a first-class status, never an invented value.
    gap = ExtractedField[str]()
    assert gap.status == "not_found" and gap.value is None


# ---- evidence_items() seam -----------------------------------------------------


def _lab_extraction() -> LabReportExtraction:
    return LabReportExtraction(
        document_id=42,
        collection_date=ExtractedField[str](
            value="2026-07-01", page=1, verbatim_quote="Collected: 2026-07-01",
            status="extracted",
        ),
        lab_values=[
            ExtractedLabValue(
                field_id="glucose",
                name=ExtractedField[str](value="Glucose", page=1, status="extracted"),
                value=ExtractedField[str](value="105", page=1, bbox=_bbox(),
                                          verbatim_quote="Glucose 105 mg/dL",
                                          status="extracted"),
                unit=ExtractedField[str](value="mg/dL", page=1, status="extracted"),
            ),
            # Verification failed → must NOT become evidence (fail-closed).
            ExtractedLabValue(
                field_id="a1c",
                name=ExtractedField[str](value="Hemoglobin A1c", page=1, status="extracted"),
                value=ExtractedField[str](value="9.9", page=1, status="failed_verification"),
            ),
        ],
    )


def test_lab_evidence_items_shape():
    """FAILURE MODE GUARDED: a failed/not-found field leaking into the evidence
    seam would let the answerer cite an unverified value as chart fact."""
    items = _lab_extraction().evidence_items()
    by_field = {ref.field_id: (ref, text) for ref, text in items}

    assert set(by_field) == {"collection_date", "glucose"}  # a1c excluded
    ref, text = by_field["glucose"]
    assert isinstance(ref, DocumentRef)
    assert ref.document_id == 42 and ref.page == 1
    assert ref.quote == "Glucose 105 mg/dL"
    assert text == "Glucose 105 mg/dL"  # normalized "name value unit"


def test_lab_evidence_items_requires_document_id():
    """FAILURE MODE GUARDED: a DocumentRef with no real documents.id would be a
    fabricated citation — the seam refuses to build one."""
    extraction = _lab_extraction()
    extraction.document_id = None
    with pytest.raises(ValueError, match="document_id"):
        extraction.evidence_items()


def test_intake_evidence_items_shape():
    intake = IntakeFormExtraction(
        document_id=7,
        chief_concern=ExtractedField[str](value="worsening fatigue", page=1,
                                          verbatim_quote="Reason for visit: worsening fatigue",
                                          status="extracted"),
        medications=[
            IntakeListItem(
                field_id="medication_1",
                name=ExtractedField[str](value="Metformin", page=2, status="extracted"),
                detail=ExtractedField[str](value="1000 mg BID", page=2, status="extracted"),
            ),
            IntakeListItem(field_id="medication_2"),  # blank row → no evidence
        ],
        allergies=[
            IntakeListItem(
                field_id="allergy_1",
                name=ExtractedField[str](value="Penicillin", page=2, status="extracted"),
            ),
        ],
    )
    items = intake.evidence_items()
    texts = {text for _ref, text in items}
    assert texts == {
        "chief concern worsening fatigue",
        "medication Metformin 1000 mg BID",
        "allergy Penicillin",
    }
    assert all(isinstance(ref, DocumentRef) and ref.document_id == 7 for ref, _ in items)


def test_ingest_result_defaults_and_prompt_version():
    res = IngestResult(correlation_id="cid-9", doc_type="lab_report", status="persisted")
    assert res.persisted == [] and res.gaps == [] and res.needs_review == []
    assert res.deduplicated is False
    assert res.prompt_version == PROMPT_VERSION == "w2-extract-v1"


# ---- medication_list (third document type, T4) -----------------------------------


def _medlist_extraction() -> MedicationListExtraction:
    def f(value: str, quote: str) -> ExtractedField[str]:
        return ExtractedField[str](value=value, page=1, verbatim_quote=quote,
                                   status="extracted")

    met_quote = "Metformin 1000 mg oral BID Active Chen"
    return MedicationListExtraction(
        document_id=55,
        patient_name=f("TESTPATIENT, ALPHA", "Patient: TESTPATIENT, ALPHA"),
        list_date=f("2026-07-10", "List Date: 2026-07-10"),
        medications=[
            MedicationListItem(
                field_id="metformin",
                name=f("Metformin", met_quote),
                strength=f("1000 mg", met_quote),
                route=f("oral", met_quote),
                frequency=f("BID", met_quote),
                status=f("Active", met_quote),
                prescriber=f("Chen", met_quote),
            ),
            # name failed verification -> the whole row must yield NO evidence
            MedicationListItem(
                field_id="ghostodone",
                name=ExtractedField[str](value="Ghostodone", page=1,
                                         status="failed_verification"),
                strength=f("50 mg", "Ghostodone 50 mg"),
            ),
            MedicationListItem(field_id="blank_row"),  # all not_found -> no evidence
        ],
    )


def test_medication_list_round_trip():
    """FAILURE MODE GUARDED: a med-list extraction losing statuses/quotes through
    a serialize->parse round trip would break verification and provenance."""
    ext = _medlist_extraction()
    back = MedicationListExtraction.model_validate_json(ext.model_dump_json())
    assert back.doc_type == "medication_list"
    assert back.medications[0].frequency.value == "BID"
    assert back.medications[0].frequency.status == "extracted"
    assert back.medications[1].name.status == "failed_verification"
    assert back.medications[2].strength.status == "not_found"
    assert back.list_date.verbatim_quote == "List Date: 2026-07-10"


def test_medication_list_item_extracted_requires_value():
    """FAILURE MODE GUARDED: an 'extracted' med subfield with a null value is an
    invented fact slot — rejected at the schema boundary."""
    with pytest.raises(ValidationError):
        MedicationListItem(
            field_id="x",
            name=ExtractedField[str](status="extracted"),  # no value
        )


def test_medlist_evidence_items_shape_and_exclusions():
    """FAILURE MODE GUARDED: a failed/not-found med row leaking into the evidence
    seam would let the answerer cite an unverified medication as chart fact."""
    items = _medlist_extraction().evidence_items()
    by_field = {ref.field_id: (ref, text) for ref, text in items}

    # scalars + the ONE verified med row; failed/blank rows excluded
    assert set(by_field) == {"patient_name", "list_date", "metformin"}
    ref, text = by_field["metformin"]
    assert isinstance(ref, DocumentRef)
    assert ref.document_id == 55 and ref.page == 1
    assert ref.quote == "Metformin 1000 mg oral BID Active Chen"
    assert text == "medication Metformin 1000 mg BID"  # name + strength + frequency


def test_medlist_evidence_items_requires_document_id():
    """FAILURE MODE GUARDED: a DocumentRef with no real documents.id would be a
    fabricated citation — the seam refuses to build one."""
    ext = _medlist_extraction()
    ext.document_id = None
    with pytest.raises(ValueError, match="document_id"):
        ext.evidence_items()


def test_ingest_result_accepts_medication_list_doc_type():
    """FAILURE MODE GUARDED: IngestResult.doc_type Literal missing the new type
    would crash the service at result-shaping time on every med-list upload."""
    res = IngestResult(
        correlation_id="cid-10", doc_type="medication_list", status="persisted"
    )
    assert res.doc_type == "medication_list"
    back = IngestResult.model_validate_json(res.model_dump_json())
    assert back.doc_type == "medication_list"


# ---- Graph + retrieval records ---------------------------------------------------


def test_handoff_and_worker_result_literals():
    """FAILURE MODE GUARDED: free-text node/route names would make the handoff
    trail un-inspectable and un-loggable (the allowlist only carries enums)."""
    h = Handoff(seq=0, from_node="supervisor", to_node="evidence_retriever",
                route="route_to_evidence_retriever", reason="guideline question",
                correlation_id="cid-2", latency_ms=12.5)
    assert h.handoff_id and h.ts  # defaults are populated
    with pytest.raises(ValidationError):
        Handoff(seq=1, from_node="supervisor", to_node="some_new_agent",
                route="route_to_evidence_retriever", reason="x", correlation_id="c")
    with pytest.raises(ValidationError):
        Handoff(seq=-1, from_node="supervisor", to_node="answerer",
                route="finalize", reason="x", correlation_id="c")

    wr = WorkerResult(worker="evidence_retriever", ok=True, summary="found 3 chunks",
                      handles=["gl:0", "gl:1"], iterations=1)
    assert wr.tokens_in == 0 and wr.tokens_out == 0
    with pytest.raises(ValidationError):
        WorkerResult(worker="supervisor", ok=True, summary="not a worker")


def test_evidence_chunk_handle_prefix_and_search_result():
    """The `gl:` handle convention is what the verifier's separation rule keys
    on — it must be a stable contract constant, not a magic string."""
    chunk = EvidenceChunk(
        ref=GuidelineRef(doc_id="ada-soc", chunk_id="ada-soc#targets:0",
                         section="Targets", label="ADA SoC"),
        quote="A reasonable A1c goal for many nonpregnant adults is <7%.",
        score=0.91, source_org="ADA", source_url="https://example.org/ada-soc",
    )
    assert EvidenceChunk.HANDLE_PREFIX == "gl"
    assert chunk.evidence_text() == chunk.quote

    result = GuidelineSearchResult(chunks=[chunk], provider="tfidf", reranker="noop",
                                   degraded=True)
    round_tripped = GuidelineSearchResult.model_validate_json(result.model_dump_json())
    assert round_tripped.chunks[0].ref.chunk_id == "ada-soc#targets:0"


def test_agent_response_carries_guideline_evidence_and_handoffs():
    """New fields serialize alongside the Week 1 shape (additive, defaulted)."""
    resp = AgentResponse(
        correlation_id="cid-3",
        briefing=[],
        guideline_evidence=[Claim(text="ADA target", source_refs=[
            GuidelineRef(doc_id="ada-soc", chunk_id="ada-soc#targets:0",
                         section="Targets", label="ADA SoC")])],
        handoffs=[Handoff(seq=0, from_node="supervisor", to_node="answerer",
                          route="finalize", reason="no routing needed",
                          correlation_id="cid-3")],
    )
    wire = json.loads(resp.model_dump_json())
    assert wire["guideline_evidence"][0]["source_refs"][0]["source_type"] == "guideline"
    assert wire["handoffs"][0]["route"] == "finalize"
