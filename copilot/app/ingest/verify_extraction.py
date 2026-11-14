"""Deterministic extraction verification — the heart of D3.

For every field the VLM claims to have extracted:
1. the `verbatim_quote` must be found in the REAL text layer on the CLAIMED
   page (pypdfium2, `pdf_text.PdfTextIndex`);
2. the value token must appear INSIDE that quote (whitespace-normalized,
   casefolded containment; boolean flags are exempt — a bool has no verbatim
   token, the quote check still applies);
3. a hit replaces the VLM's bbox with the authoritative text-layer bbox:
   raw exact match -> confidence 0.95 (the >=0.9 tier), normalized-only match
   -> 0.7. Either way `origin="text_layer"` (solid border in the UI).

A miss is `failed_verification`: NOT persisted, surfaced as a gap. A document
with no text layer at all (scan) makes verification impossible: every
extracted field is downgraded to `unreadable` (needs-review path), and unless
`COPILOT_INGEST_ALLOW_UNVERIFIED` is set (default false, demo false) nothing
is persisted. A hallucinated value cannot reach the database because it has
no quote in the real text layer, and this module is code the model cannot
negotiate with.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..correlation import log_event
from .pdf_text import PdfTextIndex, normalize_text
from .schemas import (
    ExtractedField,
    IntakeFormExtraction,
    LabReportExtraction,
    MedicationListExtraction,
)

log = logging.getLogger("copilot.ingest.verify")

_CONF_EXACT = 0.95  # raw text-layer substring hit (the >=0.9 tier)
_CONF_NORMALIZED = 0.7  # whitespace-normalized-only hit


def iter_envelopes(extraction) -> list[tuple[str, str, ExtractedField]]:  # noqa: ANN001
    """Flatten an extraction into (field_id, subfield, envelope) triples.

    `field_id` is the provenance/DocumentRef id (e.g. 'cmp_glucose', 'med_1',
    'chief_concern'); `subfield` distinguishes the envelopes of a composite row
    ('' for scalars). The dotted form `field_id.subfield` is what logs and gap
    strings use — deterministic ids, never values (PHI-safe).
    """
    out: list[tuple[str, str, ExtractedField]] = []
    if isinstance(extraction, LabReportExtraction):
        out.append(("patient_name", "", extraction.patient_name))
        out.append(("collection_date", "", extraction.collection_date))
        for lv in extraction.lab_values:
            out.append((lv.field_id, "name", lv.name))
            out.append((lv.field_id, "value", lv.value))
            out.append((lv.field_id, "unit", lv.unit))
            out.append((lv.field_id, "reference_range", lv.reference_range))
            out.append((lv.field_id, "abnormal_flag", lv.abnormal_flag))
    elif isinstance(extraction, MedicationListExtraction):
        out.append(("patient_name", "", extraction.patient_name))
        out.append(("list_date", "", extraction.list_date))
        for row in extraction.medications:
            for sub in ("name", "strength", "route", "frequency", "status", "prescriber"):
                out.append((row.field_id, sub, getattr(row, sub)))
    elif isinstance(extraction, IntakeFormExtraction):
        for fid in ("patient_name", "date_of_birth", "phone", "chief_concern", "family_history"):
            out.append((fid, "", getattr(extraction, fid)))
        for row in list(extraction.medications) + list(extraction.allergies):
            out.append((row.field_id, "name", row.name))
            out.append((row.field_id, "detail", row.detail))
    else:  # pragma: no cover - contract violation upstream
        raise TypeError(f"unknown extraction type {type(extraction).__name__}")
    return out


def dotted(field_id: str, subfield: str) -> str:
    return f"{field_id}.{subfield}" if subfield else field_id


@dataclass
class VerifySummary:
    """Outcome counts after deterministic verification (post-verify statuses:
    'extracted' now means VERIFIED)."""

    no_text_layer: bool = False
    total: int = 0
    verified: int = 0
    not_found: int = 0
    failed_verification: int = 0
    unreadable: int = 0
    gaps: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)


# A comparison token is either a whole number (decimal kept intact) or an
# alphanumeric run. We split the normalized+casefolded text on every OTHER
# character (whitespace, punctuation, unit slashes, range dashes), so a value
# must line up with real tokens of the quote — never an arbitrary substring.
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[a-z0-9]+")
_PURE_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_text(text).casefold())


def _canon_num(tok: str) -> str:
    """Canonical numeric key: strip trailing zeros in the FRACTION only, so
    '8.4' == '8.40' but an integer stays distinct from its decimal form
    ('8' != '8.0'). The latter is deliberate — a bare '8' must not verify
    against the '8.0' bound of a reference range."""
    if "." not in tok:
        return tok
    intp, frac = tok.split(".", 1)
    return f"{intp}.{frac.rstrip('0') or '0'}"


def _tok_eq(a: str, b: str) -> bool:
    a_num = bool(_PURE_NUM_RE.match(a))
    b_num = bool(_PURE_NUM_RE.match(b))
    if a_num and b_num:
        return _canon_num(a) == _canon_num(b)
    if a_num or b_num:
        return False
    return a == b


def _value_in_quote(env: ExtractedField) -> bool:
    """The extracted value must match a TOKEN — or, for a multi-word value, a
    CONTIGUOUS run of tokens — of the verbatim quote. Raw substring containment
    (the old rule) let a digit-drop verify ('14' inside '142', '8' inside the
    range bound '8.0'); token alignment closes that. Booleans are exempt (no
    verbatim token exists for a flag)."""
    if isinstance(env.value, bool):
        return True
    if env.value is None:
        return False
    quote = env.verbatim_quote or ""
    v_tokens = _tokenize(str(env.value))
    if not v_tokens:
        # A pure-symbol value (e.g. a bare unit '%') has no alphanumeric token;
        # literal presence is the only sound check and symbols cannot digit-drop.
        val = normalize_text(str(env.value)).casefold()
        return bool(val) and val in normalize_text(quote).casefold()
    q_tokens = _tokenize(quote)
    n = len(v_tokens)
    for start in range(len(q_tokens) - n + 1):
        if all(_tok_eq(v_tokens[i], q_tokens[start + i]) for i in range(n)):
            return True
    return False


def verify(extraction, pdf_bytes: bytes, mimetype: str) -> VerifySummary:  # noqa: ANN001
    """Mutate the extraction in place per the rules above and return counts.

    Non-PDF uploads (PNG/JPEG images) have no text layer by definition and take
    the same needs-review path as a scan.
    """
    envelopes = iter_envelopes(extraction)
    summary = VerifySummary(total=len(envelopes))

    idx: PdfTextIndex | None = None
    try:
        if mimetype == "application/pdf":
            idx = PdfTextIndex(pdf_bytes)
            has_text = idx.has_text_layer()
        else:
            has_text = False

        if not has_text:
            summary.no_text_layer = True
            for fid, sub, env in envelopes:
                if env.status == "extracted":
                    env.status = "unreadable"
                    env.confidence = None  # VLM-only; no deterministic support
                    summary.unreadable += 1
                    summary.needs_review.append(dotted(fid, sub))
                elif env.status == "not_found":
                    summary.not_found += 1
                    summary.gaps.append(f"{dotted(fid, sub)}: not_found")
                else:
                    summary.unreadable += 1
                    summary.needs_review.append(dotted(fid, sub))
                log_event(
                    log, "extract_field_outcome",
                    field_id=dotted(fid, sub), status=env.status,
                )
            return summary

        for fid, sub, env in envelopes:
            if env.status == "not_found":
                summary.not_found += 1
                summary.gaps.append(f"{dotted(fid, sub)}: not_found")
            elif env.status == "extracted":
                match = None
                if env.verbatim_quote and env.page:
                    value_hint = (
                        str(env.value)
                        if env.value is not None and not isinstance(env.value, bool)
                        else None
                    )
                    match = idx.find_quote(env.page, env.verbatim_quote, value=value_hint)
                if match is not None and _value_in_quote(env):
                    env.bbox = match.bbox  # authoritative, origin="text_layer"
                    env.confidence = _CONF_EXACT if match.exact else _CONF_NORMALIZED
                    summary.verified += 1
                else:
                    env.status = "failed_verification"
                    env.confidence = None
                    # keep any VLM bbox (origin already coerced to "vlm") as a
                    # display hint; it is never persisted.
                    summary.failed_verification += 1
                    reason = (
                        "value not inside quote"
                        if match is not None
                        else "quote not found in text layer"
                    )
                    summary.gaps.append(
                        f"{dotted(fid, sub)}: failed_verification ({reason})"
                    )
            else:  # 'unreadable' / 'failed_verification' straight from the VLM
                env.status = "unreadable" if env.status == "unreadable" else env.status
                summary.unreadable += 1
                summary.needs_review.append(dotted(fid, sub))
            log_event(
                log, "extract_field_outcome",
                field_id=dotted(fid, sub), status=env.status, confidence=env.confidence,
            )
        return summary
    finally:
        if idx is not None:
            idx.close()
