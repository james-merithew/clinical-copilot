"""Ingestion contracts (Week 2, design decision D3) — extraction without
invention.

Every extracted value travels in an `ExtractedField` envelope: the model must
say WHERE it read the value (page/bbox) and QUOTE the text it read — the
claims that make it falsifiable. Deterministic verification (pypdfium2 text
layer) then either confirms the field or fails it closed; an unverified value
is never persisted (`failed_verification` → surfaced as a gap).

These models double as the VLM's forced-tool `input_schema` (the model cannot
answer in prose, only fill the schema) and as the shape `model_validate`
re-checks on every retry. `PROMPT_VERSION` stamps provenance rows so extraction
behavior changes are traceable.
"""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator

from ..contracts import AnySourceRef, BBox, DocumentRef

# Stamped into provenance/logs so a prompt change is a visible lineage event.
PROMPT_VERSION = "w2-extract-v1"

# `extracted`            — model produced a value + quote; pending/passed text-layer check
# `not_found`            — the document simply lacks this field (a gap, never a guess)
# `unreadable`           — page/region has no usable text layer (e.g. scanned) → needs review
# `failed_verification`  — quote/value not found in the real text layer → NEVER persisted
FieldStatus = Literal["extracted", "not_found", "unreadable", "failed_verification"]

T = TypeVar("T")


class ExtractedField(BaseModel, Generic[T]):
    """The per-field envelope: a value is worthless without where-it-was-read
    and the verbatim text that contains it."""

    value: T | None = None
    page: int | None = Field(None, ge=1, description="1-based page the value was read from")
    bbox: BBox | None = None
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    verbatim_quote: str | None = Field(
        None, description="Exact text-layer snippet the value was read from"
    )
    status: FieldStatus = "not_found"

    @model_validator(mode="after")
    def _extracted_requires_value(self) -> "ExtractedField[T]":
        if self.status == "extracted" and self.value is None:
            raise ValueError("status='extracted' requires a non-null value")
        return self


def _empty_str_field() -> ExtractedField[str]:
    return ExtractedField[str](status="not_found")


def _empty_bool_field() -> ExtractedField[bool]:
    return ExtractedField[bool](status="not_found")


def _norm(*parts: object) -> str:
    """Normalized single-space evidence text from non-empty parts."""
    return " ".join(str(p).strip() for p in parts if p is not None and str(p).strip())


class ExtractedLabValue(BaseModel):
    """One analyte row of a lab report. `field_id` is the deterministic id used
    by `copilot.provenance` (UNIQUE(document_id, field_id)) and by DocumentRef."""

    field_id: str = Field(..., description="Deterministic field id, e.g. 'glucose'")
    name: ExtractedField[str] = Field(default_factory=_empty_str_field)
    value: ExtractedField[str] = Field(default_factory=_empty_str_field)
    unit: ExtractedField[str] = Field(default_factory=_empty_str_field)
    reference_range: ExtractedField[str] = Field(default_factory=_empty_str_field)
    abnormal_flag: ExtractedField[bool] = Field(default_factory=_empty_bool_field)


class IntakeListItem(BaseModel):
    """One medication or allergy line from an intake form."""

    field_id: str = Field(..., description="Deterministic field id, e.g. 'medication_1'")
    name: ExtractedField[str] = Field(default_factory=_empty_str_field)
    # dose/frequency (medications) or reaction (allergies), as written
    detail: ExtractedField[str] = Field(default_factory=_empty_str_field)


class MedicationListItem(BaseModel):
    """One row of a medication-list document (pharmacy printout style).

    Every subfield rides its own envelope so a misread strength can fail
    verification alone while the verified name still persists. All strings are
    kept exactly as printed — abbreviations (BID, QHS) are never expanded."""

    field_id: str = Field(..., description="Deterministic field id, e.g. 'metformin'")
    name: ExtractedField[str] = Field(default_factory=_empty_str_field)
    strength: ExtractedField[str] = Field(default_factory=_empty_str_field)
    route: ExtractedField[str] = Field(default_factory=_empty_str_field)
    frequency: ExtractedField[str] = Field(default_factory=_empty_str_field)
    status: ExtractedField[str] = Field(default_factory=_empty_str_field)
    prescriber: ExtractedField[str] = Field(default_factory=_empty_str_field)


def _doc_ref(document_id: int, field_id: str, f: ExtractedField, label: str) -> DocumentRef:
    return DocumentRef(
        document_id=document_id,
        page=f.page,
        field_id=field_id,
        bbox=f.bbox,
        quote=f.verbatim_quote,
        label=label,
    )


class _ExtractionBase(BaseModel):
    """Shared plumbing for extraction models.

    `document_id` is set by the ingestion SERVICE after the document row exists
    (the doc is stored before extraction runs) — it is never produced by the
    VLM and is excluded from the forced-tool schema by the extractor.
    """

    document_id: int | None = None

    def _require_document_id(self) -> int:
        if self.document_id is None:
            raise ValueError(
                "evidence_items() requires document_id — set by the ingestion "
                "service once the document row exists, never by the VLM"
            )
        return self.document_id


class LabReportExtraction(_ExtractionBase):
    """Forced-tool schema for lab-report PDFs (required fields per assignment:
    test name, value, unit, reference range, collection date, abnormal flag)."""

    doc_type: Literal["lab_report"] = "lab_report"
    patient_name: ExtractedField[str] = Field(default_factory=_empty_str_field)
    collection_date: ExtractedField[str] = Field(default_factory=_empty_str_field)
    lab_values: list[ExtractedLabValue] = Field(default_factory=list)

    def evidence_items(self) -> list[tuple[AnySourceRef, str]]:
        """The seam the intake-extractor worker registers into the EvidenceSet:
        one (DocumentRef, normalized "name value unit" text) per extracted-status
        field. Non-extracted fields never become evidence (gaps stay gaps)."""
        doc_id = self._require_document_id()
        items: list[tuple[AnySourceRef, str]] = []
        for fid, f in (("patient_name", self.patient_name), ("collection_date", self.collection_date)):
            if f.status == "extracted":
                items.append(
                    (_doc_ref(doc_id, fid, f, f"Lab report — {fid.replace('_', ' ')}"),
                     _norm(fid.replace("_", " "), f.value))
                )
        for lv in self.lab_values:
            if lv.value.status != "extracted" or lv.name.status != "extracted":
                continue  # a lab value without a verified name+value is a gap, not evidence
            text = _norm(
                lv.name.value,
                lv.value.value,
                lv.unit.value if lv.unit.status == "extracted" else None,
            )
            items.append(
                (_doc_ref(doc_id, lv.field_id, lv.value, f"Lab report — {lv.name.value}"), text)
            )
        return items


class IntakeFormExtraction(_ExtractionBase):
    """Forced-tool schema for patient intake forms (required fields per
    assignment: demographics, chief concern, medications, allergies, family
    history). Demographics are compare-and-flag only — never written back."""

    doc_type: Literal["intake_form"] = "intake_form"
    patient_name: ExtractedField[str] = Field(default_factory=_empty_str_field)
    date_of_birth: ExtractedField[str] = Field(default_factory=_empty_str_field)
    phone: ExtractedField[str] = Field(default_factory=_empty_str_field)
    chief_concern: ExtractedField[str] = Field(default_factory=_empty_str_field)
    family_history: ExtractedField[str] = Field(default_factory=_empty_str_field)
    medications: list[IntakeListItem] = Field(default_factory=list)
    allergies: list[IntakeListItem] = Field(default_factory=list)

    def evidence_items(self) -> list[tuple[AnySourceRef, str]]:
        """One (DocumentRef, normalized "name value" text) per extracted-status
        field — same seam as LabReportExtraction."""
        doc_id = self._require_document_id()
        items: list[tuple[AnySourceRef, str]] = []
        scalars = (
            ("patient_name", self.patient_name),
            ("date_of_birth", self.date_of_birth),
            ("phone", self.phone),
            ("chief_concern", self.chief_concern),
            ("family_history", self.family_history),
        )
        for fid, f in scalars:
            if f.status == "extracted":
                items.append(
                    (_doc_ref(doc_id, fid, f, f"Intake form — {fid.replace('_', ' ')}"),
                     _norm(fid.replace("_", " "), f.value))
                )
        for kind, rows in (("medication", self.medications), ("allergy", self.allergies)):
            for row in rows:
                if row.name.status != "extracted":
                    continue
                text = _norm(
                    kind,
                    row.name.value,
                    row.detail.value if row.detail.status == "extracted" else None,
                )
                items.append(
                    (_doc_ref(doc_id, row.field_id, row.name, f"Intake form — {row.name.value}"), text)
                )
        return items


class MedicationListExtraction(_ExtractionBase):
    """Forced-tool schema for medication-list documents (third doc type, T4).

    A document-reported med list is patient/pharmacy testimony, NOT a
    prescriber order — extracted rows persist to `lists` (type='medication'),
    the same surface as intake current-meds, and NEVER to `prescriptions`
    (which `tools/medications.py` treats as the ordered-authority row; see the
    AUDIT.md divergence rule)."""

    doc_type: Literal["medication_list"] = "medication_list"
    patient_name: ExtractedField[str] = Field(default_factory=_empty_str_field)
    list_date: ExtractedField[str] = Field(default_factory=_empty_str_field)
    medications: list[MedicationListItem] = Field(default_factory=list)

    def evidence_items(self) -> list[tuple[AnySourceRef, str]]:
        """One (DocumentRef, normalized text) per extracted-status field — the
        same seam as the other extraction models. Only rows whose NAME is
        extracted yield evidence (gaps stay gaps, never invented)."""
        doc_id = self._require_document_id()
        items: list[tuple[AnySourceRef, str]] = []
        for fid, f in (("patient_name", self.patient_name), ("list_date", self.list_date)):
            if f.status == "extracted":
                items.append(
                    (_doc_ref(doc_id, fid, f, f"Medication list — {fid.replace('_', ' ')}"),
                     _norm(fid.replace("_", " "), f.value))
                )
        for row in self.medications:
            if row.name.status != "extracted":
                continue
            text = _norm(
                "medication",
                row.name.value,
                row.strength.value if row.strength.status == "extracted" else None,
                row.frequency.value if row.frequency.status == "extracted" else None,
            )
            items.append(
                (_doc_ref(doc_id, row.field_id, row.name, f"Medication list — {row.name.value}"),
                 text)
            )
        return items


IngestStatus = Literal["persisted", "deduplicated", "needs_review", "failed"]


class IngestResult(BaseModel):
    """What `POST /ingest` returns — the upload result card's contract:
    persisted facts w/ citations, gaps stated in words, explicit dedup notice."""

    correlation_id: str
    doc_type: Literal["lab_report", "intake_form", "medication_list"]
    status: IngestStatus
    document_id: int | None = None
    deduplicated: bool = False  # UNIQUE(content_hash, pid) hit → zero new rows
    # Citations for every fact actually written (DocumentRef for the document
    # region, SourceRef for the chart row it became — the union carries both).
    persisted: list[AnySourceRef] = Field(default_factory=list)
    # Gaps stated in words, never inferred: not_found / failed_verification fields.
    gaps: list[str] = Field(default_factory=list)
    # Fields shown for human review only (e.g. scanned doc, no text layer).
    needs_review: list[str] = Field(default_factory=list)
    fields_total: int = 0
    fields_extracted: int = 0
    fields_not_found: int = 0
    fields_failed_verification: int = 0
    prompt_version: str = PROMPT_VERSION
    notes: str | None = None
