"""Evidence set — the mechanism behind the hard source-attribution invariant.

Flow (Gate 0 hardening):
1. Tools return typed records, each with a real `SourceRef` (DB PK) + an
   `evidence_text()`.
2. The agent registers each record here, getting back a short **opaque handle**
   (`"med:0"`). The model only ever sees handles + evidence text — never raw PKs,
   which it would mistype.
3. The model cites handles in its `submit_briefing` claims.
4. The Verifier maps handle→SourceRef and checks (a) provenance (the handle was
   really issued this turn) and (b) field-grounding (the claim's salient tokens
   appear in the cited evidence) — so a claim citing the *wrong* row fails closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .contracts import AnySourceRef

_NUM = re.compile(r"\d+(?:\.\d+)?")
_WORD = re.compile(r"[a-z]{4,}")
_STOP = {
    "this", "that", "with", "from", "have", "there", "their", "were", "which",
    "patient", "recent", "since", "visit", "active", "current", "last", "note",
    "record", "value", "values", "date", "dates", "today", "history", "shows",
    "started", "taking", "reason", "based", "chart", "review", "needs", "flag",
}


_UNIT = r"(?:mg|mcg|g|ml|mmol/l|mg/dl|units?|iu|meq|ng/ml|mmhg|kg|lb)"
_VALUE_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s?(?:" + _UNIT + r"|%)\b",
    re.IGNORECASE,
)
_DECIMAL = re.compile(r"\b\d+\.\d+\b")

# A bare integer that is a CLINICALLY-MEANINGFUL VALUE ('Glucose was 300',
# 'eGFR is 30', 'Sodium 138') — the unitless lab values a hallucination gets
# wrong. Excluded, because they are NOT patient values a fabrication tests:
#   - dates/years: a standalone integer must not touch a date separator, and a
#     year is 4 digits (the 1-3 digit cap drops it — '2019' never matches);
#   - percentages/targets: '7 %' is a guideline/threshold reference, not a
#     patient value (and _VALUE_UNIT already owns real unit values like '10 mg');
#   - counts: '3 (active) medications' is a count, not a lab value.
_COUNT_NOUN = (
    r"(?:medications?|meds?|problems?|allerg(?:y|ies)|diagnos[ei]s|conditions?|"
    r"results?|labs?|visits?|encounters?|prescriptions?|refills?|doses?|"
    r"entries|items?|records?|values?|readings?)"
)
_BARE_VALUE_INT = re.compile(
    r"(?<![\w./-])(\d{1,3})(?![\w./-])"                       # standalone 1-3 digit int
    r"(?!\s?%)"                                                # not a percentage
    r"(?!\s?" + _UNIT + r"\b)"                                 # not a unit value
    r"(?!(?:\s+\w+){0,2}\s+" + _COUNT_NOUN + r"\b)",           # not a count (N [adj] noun)
    re.IGNORECASE,
)
# An integer that ORDINALLY CLASSIFIES the following term is not a value:
# 'Type 2 diabetes', 'Stage 3 CKD', 'Grade 2', 'Class II', 'CKD stage 3'.
_CLASSIFIER_BEFORE = re.compile(
    r"(?:type|stage|grade|class|phase|level|tier|category|factor|line|step)\s+$",
    re.IGNORECASE,
)


def numbers(text: str) -> set[str]:
    # Numbers are the load-bearing, hallucination-prone tokens (doses, lab values).
    return set(_NUM.findall(text or ""))


def value_numbers(text: str) -> set[str]:
    """The 'value-like' numbers a hallucination gets wrong: a number with a
    clinical unit (10 mg) or a decimal (8.2), PLUS a standalone clinically-
    meaningful bare integer (an unitless lab value like 'Sodium 138'). Counts
    ('3 medications'), years ('2019'), date parts, percentage/threshold
    references ('7 %'), and ordinal classifiers ('Type 2 diabetes') stay exempt
    so legitimate summaries aren't false-quarantined — see the regexes above."""
    t = text or ""
    vals = set(_VALUE_UNIT.findall(t))
    vals |= set(_DECIMAL.findall(t))
    for m in _BARE_VALUE_INT.finditer(t):
        if _CLASSIFIER_BEFORE.search(t[: m.start()]):
            continue  # 'Type 2', 'Stage 3' — a classifier, not a value
        vals.add(m.group(1))
    return vals


def keywords(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


@dataclass
class EvidenceItem:
    handle: str
    # Week 2: any citation kind flows through the same handle discipline —
    # patient rows (SourceRef), corpus chunks (GuidelineRef, `gl:` handles),
    # document regions (DocumentRef). Type widened only; behavior unchanged.
    source_ref: AnySourceRef
    text: str  # canonical evidence string (synthetic-PHI only)


@dataclass
class EvidenceSet:
    """Accumulates every record surfaced to the model this turn."""

    items: dict[str, EvidenceItem] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def add(self, prefix: str, source_ref: AnySourceRef, text: str) -> str:
        n = self._counters.get(prefix, 0)
        self._counters[prefix] = n + 1
        handle = f"{prefix}:{n}"
        self.items[handle] = EvidenceItem(handle, source_ref, text)
        return handle

    def get(self, handle: str) -> EvidenceItem | None:
        return self.items.get(handle)

    def digest(self) -> list[dict[str, str]]:
        """What the model sees: handle + evidence text, no raw PKs."""
        return [{"handle": h, "data": it.text} for h, it in self.items.items()]

    def cited_text(self, handles: list[str]) -> str:
        return " ".join(
            self.items[h].text.lower() for h in handles if h in self.items
        )

    def is_empty(self) -> bool:
        return not self.items
