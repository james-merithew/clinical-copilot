"""`search_guidelines` — retrieval over the practice-guideline corpus (W2 §6.3).

Reference knowledge ONLY: this tool reads the repo's committed corpus — no DB,
no patient data, nothing user-specific. Results register in the turn's
EvidenceSet under `gl:` handles (the same handle discipline as every
patient-record tool), and the verifier's separation rule guarantees a claim
citing only `gl:` handles can never enter the patient briefing.
"""

from __future__ import annotations

from ..contracts import GuidelineSearchResult
from ..rag.index import get_index


def search_guidelines(ctx, query: str) -> GuidelineSearchResult:  # noqa: ANN001
    """`ctx` is accepted for tool-call uniformity (authorization already ran);
    the corpus is patient-independent so nothing in ctx is used."""
    return get_index().search(query)
