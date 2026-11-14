"""Hermetic tests for hybrid retrieval: RRF fusion, the degradation ladder,
and end-to-end relevance over the real corpus with the KEYLESS providers only
(BM25 + hashed TF-IDF + noop) — exactly what runs when no Cohere key exists.

Failure modes guarded:
- RRF math regression would silently reorder evidence (wrong chunk cited);
- a dense/rerank provider outage must DEGRADE (flagged), never fail the tool;
- retrieval must actually surface the right doc for the persona's bread-and-
  butter question (metformin/eGFR), keylessly — the demo floor.
"""

from __future__ import annotations

import pytest

from app.contracts import GuidelineSearchResult
from app.rag.corpus import load_corpus
from app.rag.index import HybridIndex, rrf
from app.rag.providers import LocalTfidfEmbedding, NoopReranker


class FailingEmbedder:
    name = "failing"

    def fit(self, texts):  # noqa: ANN001
        return None

    def embed_documents(self, texts):  # noqa: ANN001
        raise RuntimeError("embed down")

    def embed_query(self, text):  # noqa: ANN001
        raise RuntimeError("embed down")


class FailingReranker:
    name = "failing"

    def rerank(self, query, documents, top_n):  # noqa: ANN001
        raise RuntimeError("rerank down")


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def local_index(corpus):
    return HybridIndex(corpus, LocalTfidfEmbedding(), NoopReranker(), top_k=5)


# ---- RRF fusion ----------------------------------------------------------------


def test_rrf_fusion_orders_by_summed_reciprocal_rank():
    """Synthetic case with hand-computed scores: idx 0 (ranks 1+2) beats idx 2
    (ranks 3+1... i.e. 1/61+1/62 > 1/63+1/61), then 1, then 3."""
    fused = rrf([[0, 1, 2], [2, 0, 3]], k=60)
    assert [i for i, _ in fused] == [0, 2, 1, 3]
    scores = dict(fused)
    assert scores[0] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[3] == pytest.approx(1 / 63)


def test_rrf_ties_break_deterministically_by_index():
    fused = rrf([[7], [5]], k=60)
    assert [i for i, _ in fused] == [5, 7]


def test_rrf_empty_input():
    assert rrf([[], []]) == []


# ---- End-to-end keyless retrieval ------------------------------------------------


def test_metformin_egfr_query_hits_the_right_doc_top3(local_index):
    """RELEVANCE SMOKE (keyless floor): 'metformin eGFR cutoff' must surface
    the metformin-egfr protocol in the top 3 via BM25+TF-IDF alone."""
    res = local_index.search("metformin eGFR cutoff")
    assert isinstance(res, GuidelineSearchResult)
    assert res.degraded is False
    top3_docs = [c.ref.doc_id for c in res.chunks[:3]]
    assert "metformin-egfr" in top3_docs


def test_search_respects_top_k_and_carries_guideline_metadata(local_index):
    """CONTRACT: at most top_k chunks; every chunk carries a wellformed
    GuidelineRef + the attribution fields the UI renders."""
    res = local_index.search("blood pressure target", top_k=3)
    assert 0 < len(res.chunks) <= 3
    for c in res.chunks:
        assert c.ref.source_type == "guideline"
        assert c.ref.chunk_id.startswith(f"{c.ref.doc_id}#")
        assert c.ref.section and c.ref.label
        assert c.source_org and c.source_url
        assert c.quote == c.evidence_text()
        assert c.score > 0


def test_nonsense_query_returns_empty_not_fabricated(local_index):
    """BOUNDARY (retrieval_miss): a query with zero corpus overlap returns an
    empty result — never a fabricated 'closest' chunk with positive score."""
    res = local_index.search("zzzz qqqq xxxxyyy")
    assert res.chunks == []
    assert res.provider and res.reranker


# ---- Deterministic query expansion (recall fix, T6) -------------------------------

# Confirmed experimentally against the committed corpus with the keyless
# providers: each query MISSES its target doc in the top-3 without expansion
# and HITS with it. If the corpus or the fusion math changes these pairs,
# re-derive them — they are the headline miss→hit evidence for T6.
EXPANSION_MISS_TO_HIT = [
    ("HTN goal", "acc-aha-htn-2017"),
    ("HTN stages", "acc-aha-htn-2017"),
    ("monitoring cadence in DM", "ada-soc-glycemic"),
]


def test_abbreviation_queries_miss_top3_without_expansion(local_index, monkeypatch):
    """PRE-CHANGE BASELINE (kept as a regression sentinel): with expansion
    pinned to identity, the raw abbreviation queries do NOT reach the target
    doc in the top-3 — this is exactly the recall gap T6 closes."""
    import app.rag.index as index_mod

    with monkeypatch.context() as m:
        m.setattr(index_mod, "expand_query", lambda q: q)
        for query, target in EXPANSION_MISS_TO_HIT:
            docs = [c.ref.doc_id for c in local_index.search(query).chunks[:3]]
            assert target not in docs, f"{query!r} unexpectedly hits {target} unexpanded"


def test_abbreviation_queries_hit_top3_with_expansion(local_index):
    """RELEVANCE (T6): the same abbreviation queries surface the target doc in
    the top-3 once deterministic expansion feeds both retrieval legs."""
    for query, target in EXPANSION_MISS_TO_HIT:
        docs = [c.ref.doc_id for c in local_index.search(query).chunks[:3]]
        assert target in docs, f"{query!r} missed {target} with expansion on"


def test_full_form_queries_unregressed_by_expansion(local_index):
    """REGRESSION GUARD: full-form (no-abbreviation) queries take the identity
    path and keep their pre-T6 top-3 targets."""
    for query, target in [
        ("metformin eGFR cutoff", "metformin-egfr"),  # egfr expands — still #1 doc
        ("hypertension treatment target", "acc-aha-htn-2017"),  # identity path
        ("statin primary prevention", "ascvd-statin-primary"),  # identity path
    ]:
        docs = [c.ref.doc_id for c in local_index.search(query).chunks[:3]]
        assert target in docs


def test_reranker_receives_the_original_query_not_the_expanded_one(corpus):
    """INVARIANT (precision anchor): expansion fixes recall in the retrieval
    legs, but the reranker must rank against the user's OWN words — never the
    synonym-padded string."""

    class QueryCapturingReranker:
        name = "capture"

        def __init__(self) -> None:
            self.queries: list[str] = []

        def rerank(self, query, documents, top_n):  # noqa: ANN001
            self.queries.append(query)
            return list(range(min(top_n, len(documents))))

    cap = QueryCapturingReranker()
    idx = HybridIndex(corpus, LocalTfidfEmbedding(), cap, top_k=5)
    idx.search("HTN goal")
    assert cap.queries == ["HTN goal"]


# ---- Degradation ladder ----------------------------------------------------------


def test_embed_failure_at_build_degrades_to_bm25_only(corpus):
    """INVARIANT (graceful degradation): dense build failure → BM25-only
    retrieval with degraded=True — search still answers."""
    idx = HybridIndex(corpus, FailingEmbedder(), NoopReranker(), top_k=5)
    assert idx.dense_ok is False
    res = idx.search("metformin eGFR cutoff")
    assert res.degraded is True
    assert res.provider == "bm25"
    assert any(c.ref.doc_id == "metformin-egfr" for c in res.chunks[:3])


def test_reranker_failure_falls_back_to_noop_and_flags_degraded(corpus):
    """INVARIANT (fallback ladder): a reranker error mid-search → NoopReranker
    order + degraded=True, never an exception out of the tool."""
    idx = HybridIndex(corpus, LocalTfidfEmbedding(), FailingReranker(), top_k=5)
    res = idx.search("statin therapy")
    assert res.degraded is True
    assert res.reranker == "noop"
    assert res.chunks  # RRF order stands
