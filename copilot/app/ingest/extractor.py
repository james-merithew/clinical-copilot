"""Vision extraction — the VLM proposes, deterministic code disposes (D3).

One Anthropic vision call per document with a single FORCED tool
(`submit_extraction`) whose `input_schema` is derived from the Pydantic
extraction model — the model cannot answer in prose, only fill the schema.
`model_validate` re-checks every reply; schema-invalid output is re-prompted
with the validation error at most `settings.extract_max_retries` times, then
ingestion fails explicitly (`extract_invalid_schema`, §11) — invalid output
never propagates.

Trust boundaries enforced here:
- `document_id` is STRIPPED from the tool schema and from the reply — it is
  assigned by the ingestion service after the document row exists, never by
  the model (see `_ExtractionBase`).
- Any bbox the model supplies is coerced to `origin="vlm"` — the model cannot
  claim text-layer authority for its own guesses; only `verify_extraction`
  grants that.
- Mock mode (`settings.mock_llm`) returns a canned minimal extraction so
  hermetic tests and load tests never touch the API.
"""

from __future__ import annotations

import base64
import copy
import logging
from typing import Any

from pydantic import ValidationError

from ..config import Settings, get_settings
from ..correlation import log_event
from .schemas import (
    ExtractedField,
    ExtractedLabValue,
    IntakeFormExtraction,
    IntakeListItem,
    LabReportExtraction,
    MedicationListExtraction,
    MedicationListItem,
    PROMPT_VERSION,
)
from .verify_extraction import iter_envelopes

log = logging.getLogger("copilot.ingest.extractor")

EXTRACTION_MODELS: dict[str, type] = {
    "lab_report": LabReportExtraction,
    "intake_form": IntakeFormExtraction,
    "medication_list": MedicationListExtraction,
}

_TOOL_NAME = "submit_extraction"

# 35s per vision call inside the 30s+ ingestion SLO (off the critical path,
# W2_ARCHITECTURE §2.1). A real PDF document block takes ~8-25s server-side;
# 20s was too tight and produced silent APITimeoutErrors under live load.
# 1 transient (network/5xx) retry at the SDK level.
_VISION_TIMEOUT_S = 35.0
_SDK_TRANSIENT_RETRIES = 1

# --- Flat forced-tool schemas -------------------------------------------------
# The VLM fills a FLAT schema — plain fields, no $ref/$defs, no generic
# `ExtractedField[T]` indirection. A real model chokes on the deeply-nested
# schema derived from the Pydantic contracts (it emits placeholder keys and
# empty arrays); a flat schema it fills reliably and faster. The rich contracts
# stay the source of truth: `_map_*` CONSTRUCTS validated ExtractedField
# envelopes from the flat output — we never trust the model's structure, only
# its field values, each of which still faces deterministic text-layer
# verification downstream.

_VALUE_FIELD = {
    "type": "object",
    "properties": {
        "value": {"type": "string", "description": "the value exactly as printed; omit if absent"},
        "verbatim_quote": {
            "type": "string",
            "description": "the exact text-layer line containing the value, character-for-character",
        },
        "page": {"type": "integer", "description": "1-based page the value appears on"},
    },
    "required": ["value", "verbatim_quote", "page"],
}

_LAB_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "patient_name": _VALUE_FIELD,
        "collection_date": _VALUE_FIELD,
        "lab_values": {
            "type": "array",
            "description": "one entry per analyte row in the report",
            "items": {
                "type": "object",
                "properties": {
                    "test_name": {"type": "string"},
                    "value": {"type": "string"},
                    "unit": {"type": "string"},
                    "reference_range": {"type": "string"},
                    "abnormal_flag": {
                        "type": "string",
                        "description": "H, L, HH, LL, or empty for a normal result",
                    },
                    "verbatim_quote": {
                        "type": "string",
                        "description": "the exact line from the document, character-for-character",
                    },
                    "page": {"type": "integer"},
                },
                "required": ["test_name", "value", "verbatim_quote", "page"],
            },
        },
    },
    "required": ["lab_values"],
}

_INTAKE_LIST_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "detail": {"type": "string", "description": "dose/frequency (meds) or reaction (allergies)"},
        "verbatim_quote": {"type": "string"},
        "page": {"type": "integer"},
    },
    "required": ["name", "verbatim_quote", "page"],
}

_INTAKE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "patient_name": _VALUE_FIELD,
        "date_of_birth": _VALUE_FIELD,
        "phone": _VALUE_FIELD,
        "chief_concern": _VALUE_FIELD,
        "family_history": _VALUE_FIELD,
        "medications": {"type": "array", "items": _INTAKE_LIST_ITEM},
        "allergies": {"type": "array", "items": _INTAKE_LIST_ITEM},
    },
    "required": [],
}

_MEDLIST_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "strength": {"type": "string", "description": "e.g. '1000 mg', exactly as printed"},
        "route": {"type": "string", "description": "e.g. 'oral', exactly as printed"},
        "frequency": {
            "type": "string",
            "description": "exactly as printed — keep abbreviations (BID stays BID)",
        },
        "status": {"type": "string", "description": "e.g. Active / Discontinued, as printed"},
        "prescriber": {"type": "string"},
        "verbatim_quote": {
            "type": "string",
            "description": "the exact medication row line, character-for-character",
        },
        "page": {"type": "integer"},
    },
    "required": ["name", "verbatim_quote", "page"],
}

_MEDLIST_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "patient_name": _VALUE_FIELD,
        "list_date": _VALUE_FIELD,
        "medications": {
            "type": "array",
            "description": "one entry per medication row in the list",
            "items": _MEDLIST_ITEM,
        },
    },
    "required": ["medications"],
}

_FLAT_SCHEMAS: dict[str, dict[str, Any]] = {
    "lab_report": _LAB_TOOL_SCHEMA,
    "intake_form": _INTAKE_TOOL_SCHEMA,
    "medication_list": _MEDLIST_TOOL_SCHEMA,
}

_SYSTEM = (
    f"You extract structured data from clinical documents (prompt {PROMPT_VERSION}). "
    "Fill the submit_extraction tool schema and nothing else. Rules: "
    "(1) For every field you extract, set status='extracted', copy the EXACT verbatim text "
    "snippet containing the value into verbatim_quote, and give the 1-based page it appears on. "
    "(2) NEVER invent a value: if the document does not contain a field, set status='not_found' "
    "and leave value null. (3) If a region is illegible or the page is a scan you cannot read "
    "reliably, use status='unreadable'. (4) Quotes must be copied character-for-character from "
    "the document — they are verified against the real text layer and any mismatch discards the "
    "field. (5) bbox values are normalized 0-1 page coordinates with origin='vlm'."
)


class ExtractionInvalid(Exception):
    """The VLM never produced schema-valid output within the retry budget."""

    def __init__(self, retries: int, last_error: str) -> None:
        super().__init__(f"extraction invalid after {retries} retries")
        self.retries = retries
        self.last_error = last_error


class ExtractionUnavailable(Exception):
    """The provider call itself failed (transport/auth), after SDK retries."""


def tool_schema(doc_type: str) -> dict[str, Any]:
    """Forced-tool input_schema — the FLAT, model-friendly schema for `doc_type`
    (NOT the nested Pydantic-derived schema, which a real VLM cannot fill)."""
    return copy.deepcopy(_FLAT_SCHEMAS[doc_type])


def _slug(text: str, seq: int) -> str:
    """Deterministic field_id from a test/med name (provenance UNIQUE key)."""
    base = "".join(c if c.isalnum() else "_" for c in (text or "").lower()).strip("_")
    base = "_".join(p for p in base.split("_") if p)
    return base or f"field_{seq}"


def _str_field(raw: dict | None) -> ExtractedField:
    """Map a flat {value, verbatim_quote, page} object into an ExtractedField[str].
    A missing / empty value is a gap (not_found), never an invented value."""
    if not isinstance(raw, dict):
        return ExtractedField[str](status="not_found")
    val = raw.get("value")
    if val is None or str(val).strip() == "":
        return ExtractedField[str](status="not_found")
    return ExtractedField[str](
        value=str(val).strip(),
        verbatim_quote=(raw.get("verbatim_quote") or None),
        page=raw.get("page"),
        status="extracted",
    )


def _plain_str_field(value, quote, page) -> ExtractedField:  # noqa: ANN001
    if value is None or str(value).strip() == "":
        return ExtractedField[str](status="not_found")
    return ExtractedField[str](
        value=str(value).strip(), verbatim_quote=(quote or None), page=page, status="extracted"
    )


_ABNORMAL_TOKENS = {"h", "l", "hh", "ll", "a", "high", "low", "abnormal", "crit", "critical"}


def _flag_field(flag, quote, page) -> ExtractedField:  # noqa: ANN001
    """abnormal_flag is a bool envelope: H/L/HH/LL/etc → True, blank → False.
    Carries the analyte line's quote+page so it faces text-layer verification
    (bools are value-exempt but the quote must still be real)."""
    if not quote:
        return ExtractedField[bool](status="not_found")
    is_abn = str(flag or "").strip().lower() in _ABNORMAL_TOKENS
    return ExtractedField[bool](value=is_abn, verbatim_quote=quote, page=page, status="extracted")


def _map_lab(payload: dict) -> LabReportExtraction:
    lab_values: list[ExtractedLabValue] = []
    for i, row in enumerate(payload.get("lab_values") or []):
        if not isinstance(row, dict):
            continue
        name = row.get("test_name")
        quote = row.get("verbatim_quote")
        page = row.get("page")
        lab_values.append(
            ExtractedLabValue(
                field_id=_slug(str(name), i),
                name=_plain_str_field(name, quote, page),
                value=_plain_str_field(row.get("value"), quote, page),
                unit=_plain_str_field(row.get("unit"), quote, page),
                reference_range=_plain_str_field(row.get("reference_range"), quote, page),
                abnormal_flag=_flag_field(row.get("abnormal_flag"), quote, page),
            )
        )
    return LabReportExtraction(
        patient_name=_str_field(payload.get("patient_name")),
        collection_date=_str_field(payload.get("collection_date")),
        lab_values=lab_values,
    )


def _map_intake(payload: dict) -> IntakeFormExtraction:
    def rows(key: str, prefix: str) -> list[IntakeListItem]:
        out: list[IntakeListItem] = []
        for i, row in enumerate(payload.get(key) or []):
            if not isinstance(row, dict):
                continue
            quote = row.get("verbatim_quote")
            page = row.get("page")
            out.append(
                IntakeListItem(
                    field_id=f"{prefix}_{i + 1}",
                    name=_plain_str_field(row.get("name"), quote, page),
                    detail=_plain_str_field(row.get("detail"), quote, page),
                )
            )
        return out

    return IntakeFormExtraction(
        patient_name=_str_field(payload.get("patient_name")),
        date_of_birth=_str_field(payload.get("date_of_birth")),
        phone=_str_field(payload.get("phone")),
        chief_concern=_str_field(payload.get("chief_concern")),
        family_history=_str_field(payload.get("family_history")),
        medications=rows("medications", "medication"),
        allergies=rows("allergies", "allergy"),
    )


def _map_medlist(payload: dict) -> MedicationListExtraction:
    """Flat med-list rows → MedicationListItem envelopes. Every subfield rides
    the ROW's quote+page (like _map_lab) so each faces text-layer verification
    against the exact line the model claims to have read. field_id is
    slug(name) with an explicit collision suffix — duplicates must never
    collide on UNIQUE(document_id, field_id) in copilot.provenance."""
    meds: list[MedicationListItem] = []
    seen: dict[str, int] = {}
    for i, row in enumerate(payload.get("medications") or []):
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        quote = row.get("verbatim_quote")
        page = row.get("page")
        fid = _slug(str(name), i)
        n = seen.get(fid, 0) + 1
        seen[fid] = n
        if n > 1:
            fid = f"{fid}_{n}"
        meds.append(
            MedicationListItem(
                field_id=fid,
                name=_plain_str_field(name, quote, page),
                strength=_plain_str_field(row.get("strength"), quote, page),
                route=_plain_str_field(row.get("route"), quote, page),
                frequency=_plain_str_field(row.get("frequency"), quote, page),
                status=_plain_str_field(row.get("status"), quote, page),
                prescriber=_plain_str_field(row.get("prescriber"), quote, page),
            )
        )
    return MedicationListExtraction(
        patient_name=_str_field(payload.get("patient_name")),
        list_date=_str_field(payload.get("list_date")),
        medications=meds,
    )


_MAPPERS = {
    "lab_report": _map_lab,
    "intake_form": _map_intake,
    "medication_list": _map_medlist,
}

# Per-doc-type extraction hints appended to the user message (never touching
# _SYSTEM / PROMPT_VERSION — the base prompt stays w2-extract-v1).
_DOC_TYPE_HINTS: dict[str, str] = {
    "medication_list": (
        " Copy strength, route and frequency exactly as printed — never expand "
        "or normalize abbreviations (e.g. BID stays BID)."
    ),
}


def _content_block(file_bytes: bytes, mimetype: str) -> dict[str, Any]:
    data = base64.standard_b64encode(file_bytes).decode("ascii")
    if mimetype == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mimetype, "data": data},
    }


def _mock_extraction(doc_type: str):
    """Canned minimal extraction for mock mode: all fields not_found, zero
    invented values — the pipeline runs end-to-end without an API key and
    surfaces honest gaps."""
    return EXTRACTION_MODELS[doc_type]()


def _coerce_vlm_bboxes(extraction) -> None:  # noqa: ANN001
    """A model-supplied bbox is a display hint only. Whatever origin it
    claimed, stamp it 'vlm' — text_layer authority is granted exclusively by
    deterministic verification."""
    for _fid, _sub, env in iter_envelopes(extraction):
        if env.bbox is not None and env.bbox.origin != "vlm":
            env.bbox = env.bbox.model_copy(update={"origin": "vlm"})


def _default_client(s: Settings):  # noqa: ANN001
    import anthropic

    return anthropic.Anthropic(
        api_key=s.anthropic_api_key,
        timeout=_VISION_TIMEOUT_S,
        max_retries=_SDK_TRANSIENT_RETRIES,
    )


def extract(
    file_bytes: bytes,
    mimetype: str,
    doc_type: str,
    *,
    client: Any | None = None,
    settings: Settings | None = None,
):
    """Run the forced-tool vision extraction. Returns a validated extraction
    model (document_id unset — the service assigns it). Raises
    ExtractionInvalid / ExtractionUnavailable; never returns unvalidated data.
    """
    s = settings or get_settings()
    model_cls = EXTRACTION_MODELS[doc_type]

    if s.mock_llm and client is None:
        return _mock_extraction(doc_type)

    client = client or _default_client(s)
    mapper = _MAPPERS[doc_type]
    tool = {
        "name": _TOOL_NAME,
        "description": f"Submit the structured {doc_type} extraction.",
        "input_schema": tool_schema(doc_type),
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                _content_block(file_bytes, mimetype),
                {
                    "type": "text",
                    "text": f"Extract this {doc_type.replace('_', ' ')}."
                    + _DOC_TYPE_HINTS.get(doc_type, ""),
                },
            ],
        }
    ]

    max_retries = max(0, int(s.extract_max_retries))
    attempts = 1 + max_retries
    last_error = ""
    for attempt in range(attempts):
        try:
            resp = client.messages.create(
                model=(s.extract_model or s.anthropic_model),
                max_tokens=s.anthropic_max_tokens,
                system=_SYSTEM,
                tools=[tool],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - SDK already retried transients
            log_event(log, "ingest_failed", phase="extract", error=type(exc).__name__)
            raise ExtractionUnavailable(type(exc).__name__) from exc

        tool_use = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if tool_use is None:
            last_error = "no tool_use block in response"
        else:
            payload = dict(tool_use.input or {})
            payload.pop("document_id", None)  # service-assigned, never model-supplied
            try:
                # Map the FLAT model output into the rich contract, then
                # re-validate — the Pydantic model is still the source of truth.
                extraction = model_cls.model_validate(mapper(payload).model_dump())
                _coerce_vlm_bboxes(extraction)
                return extraction
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                last_error = str(exc)

        if attempt < attempts - 1:
            # Re-prompt with the validation error; tool_choice stays forced.
            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": getattr(tool_use, "id", "unknown"),
                            "content": (
                                "The extraction did not validate against the schema: "
                                f"{last_error[:2000]}\nCall submit_extraction again with a "
                                "corrected, schema-valid extraction. Do not invent values."
                            ),
                            "is_error": True,
                        }
                    ],
                },
            ]

    log_event(log, "extract_invalid_schema", retries=max_retries, doc_type=doc_type)
    raise ExtractionInvalid(retries=max_retries, last_error=last_error)
