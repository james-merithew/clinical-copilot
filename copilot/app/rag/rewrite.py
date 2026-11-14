"""Deterministic clinical query expansion — the T6 recall fix (W2 §6.2).

Physicians type abbreviations ("HTN goal", "CKD in DM"); the corpus files
often carry only the full forms. This module appends the full-form synonym
phrases for any whole-token abbreviation match to the END of the query, so
BOTH retrieval legs (BM25 and dense) see the expanded vocabulary. It is a
retrieval-side fix only:

- **Additive only.** Original tokens are never deleted or replaced — the
  output always starts with the input string, byte-for-byte.
- **Byte-deterministic.** Matches are whole tokens (via `bm25.tokenize`),
  expansion phrases are appended in sorted key order and first-seen
  deduplicated, so the same query always yields the same bytes. Retrieval
  order — and therefore which chunk gets CITED — stays reproducible.
- **Single pass, no chaining.** Keys are matched against the ORIGINAL query's
  tokens only; phrases appended by one entry never trigger another.
- **Identity on no match.** The input string is returned unchanged, so the
  common path is a cheap set lookup and `rewritten` telemetry stays honest.
- **Not re-chunking.** `chunk_id` is a citation target; the corpus, chunker,
  and ids are untouched. The reranker also never sees the expanded string —
  precision stays anchored to the user's own words (see `HybridIndex.search`).
"""

from __future__ import annotations

from .bm25 import tokenize

# Whole-token abbreviation → full-form phrases, tuned to the 9-doc corpus in
# copilot/corpus/guidelines/. Keep entries SMALL: every phrase shifts BM25
# term frequencies, and the golden evidence cases pin doc ids — shrink an
# entry before ever touching a case.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "a1c": ("hemoglobin a1c", "glycated hemoglobin"),
    "ascvd": ("atherosclerotic cardiovascular disease", "statin"),
    "bp": ("blood pressure",),
    "chf": ("heart failure",),
    "ckd": ("chronic kidney disease",),
    "dm": ("diabetes", "diabetes mellitus"),
    "egfr": ("estimated glomerular filtration rate", "kidney function"),
    "gdmt": ("guideline-directed medical therapy",),
    "hba1c": ("hemoglobin a1c", "glycated hemoglobin"),
    "hf": ("heart failure",),
    "htn": ("hypertension", "blood pressure"),
    "mi": ("myocardial infarction",),
    "t2dm": ("type 2 diabetes",),
}


def expand_query(query: str) -> str:
    """Append full-form synonyms for whole-token abbreviation matches.

    Returns the input string unchanged (identity) when nothing matches."""
    tokens = set(tokenize(query))
    additions: list[str] = []
    seen: set[str] = set()
    for key in sorted(SYNONYMS):
        if key not in tokens:
            continue
        for phrase in SYNONYMS[key]:
            if phrase not in seen:
                seen.add(phrase)
                additions.append(phrase)
    if not additions:
        return query
    return f"{query} {' '.join(additions)}"
