"""HybridIndex — BM25 top-20 ∪ dense top-20 → RRF fusion → rerank → top-k.

Built lazily on the first `search_guidelines` call (never at import — startup
and `/health` must not pay for it, and a corpus problem must not crash boot).
Degradation ladder (W2 §6.2 / §9.2): dense-embed failure → BM25-only with
`degraded=True`; reranker failure/timeout → NoopReranker with `degraded=True`.
Retrieval failure never fabricates — an empty result is an explicit miss.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Sequence

from .. import metrics_summary
from ..config import get_settings
from ..contracts import EvidenceChunk, GuidelineRef, GuidelineSearchResult
from ..correlation import log_event
from .bm25 import Bm25Index
from .corpus import Chunk, load_corpus
from .providers import (
    EmbeddingProvider,
    NoopReranker,
    Reranker,
    select_embedding,
    select_reranker,
)
from .rewrite import expand_query

log = logging.getLogger("copilot.rag")

RRF_K = 60  # standard RRF constant — rank-based, needs no score calibration
LEG_N = 20  # candidates taken from each retrieval leg before fusion


def rrf(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal-rank fusion. Input: per-leg ranked lists of chunk indices.
    Output: (index, fused_score) sorted by score desc, index asc on ties —
    fully deterministic."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda t: (-t[1], t[0]))


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class HybridIndex:
    def __init__(
        self,
        chunks: list[Chunk],
        embedder: EmbeddingProvider,
        reranker: Reranker,
        top_k: int = 5,
    ) -> None:
        self.chunks = chunks
        self.embedder = embedder
        self.reranker = reranker
        self.top_k = top_k
        self.bm25 = Bm25Index(chunks)
        self.doc_vecs: list[list[float]] = []
        self.dense_ok = False
        try:
            texts = [c.text for c in chunks]
            self.embedder.fit(texts)
            self.doc_vecs = self.embedder.embed_documents(texts)
            self.dense_ok = True
        except Exception as exc:  # noqa: BLE001 — embed failure at build → BM25-only
            log_event(
                log,
                "rag_dense_unavailable",
                provider=self.embedder.name,
                chunk_count=len(chunks),
                error=type(exc).__name__,
            )

    # -- retrieval legs --------------------------------------------------------

    def _dense_top(self, query: str, n: int) -> list[int]:
        q = self.embedder.embed_query(query)
        sims = [(_dot(q, v), i) for i, v in enumerate(self.doc_vecs)]
        sims.sort(key=lambda t: (-t[0], t[1]))
        return [i for s, i in sims[:n] if s > 0]

    # -- public API --------------------------------------------------------------

    def search(self, query: str, top_k: int | None = None) -> GuidelineSearchResult:
        k = top_k or self.top_k
        degraded = not self.dense_ok
        provider = self.embedder.name if self.dense_ok else "bm25"

        # T6 recall fix: deterministic clinical-abbreviation expansion feeds
        # BOTH retrieval legs; the reranker below still gets the ORIGINAL
        # query — precision stays anchored to the user's own words.
        expanded = expand_query(query)
        rewritten = expanded != query

        rankings: list[list[int]] = [[i for i, _ in self.bm25.top(expanded, LEG_N)]]
        if self.dense_ok:
            try:
                rankings.append(self._dense_top(expanded, LEG_N))
            except Exception as exc:  # noqa: BLE001 — query-time embed failure
                degraded, provider = True, "bm25"
                log_event(
                    log,
                    "rag_dense_unavailable",
                    provider=self.embedder.name,
                    chunk_count=len(self.chunks),
                    error=type(exc).__name__,
                )

        fused = rrf(rankings)
        if not fused:
            log_event(log, "retrieval_miss", hit=0, provider=provider, rewritten=int(rewritten))
            metrics_summary.record_retrieval(hit=False, degraded=degraded, rewritten=rewritten)
            return GuidelineSearchResult(
                chunks=[], provider=provider, reranker=self.reranker.name, degraded=degraded
            )

        pool = [i for i, _ in fused[:LEG_N]]
        score_of = dict(fused)

        reranker_name = self.reranker.name
        t0 = time.perf_counter()
        try:
            order = self.reranker.rerank(query, [self.chunks[i].text for i in pool], k)
            # Defensive normalization: valid, deduped indices only.
            seen: set[int] = set()
            order = [
                i for i in order if isinstance(i, int) and 0 <= i < len(pool) and not (i in seen or seen.add(i))
            ]
        except Exception as exc:  # noqa: BLE001 — reranker error/timeout → Noop
            degraded = True
            log_event(
                log, "reranker_fallback", rerank_provider=reranker_name, error=type(exc).__name__
            )
            reranker_name = NoopReranker.name
            order = list(range(len(pool)))
        rerank_ms = round((time.perf_counter() - t0) * 1000, 1)

        chosen = [pool[i] for i in order[:k]]
        out: list[EvidenceChunk] = []
        for idx in chosen:
            c = self.chunks[idx]
            out.append(
                EvidenceChunk(
                    ref=GuidelineRef(
                        doc_id=c.doc_id, chunk_id=c.chunk_id, section=c.section, label=c.label
                    ),
                    quote=c.text,
                    score=score_of.get(idx, 0.0),
                    source_org=c.source_org,
                    source_url=c.source_url,
                )
            )

        log_event(
            log,
            "retrieval",
            provider=provider,
            rerank_provider=reranker_name,
            rerank_ms=rerank_ms,
            retrieval_returned=len(out),
            hit=int(bool(out)),
            rewritten=int(rewritten),  # flag only — NEVER the query text (PHI)
        )
        # D6b dashboard: retrieval hit rate + degraded-mode count (never fatal).
        metrics_summary.record_retrieval(hit=bool(out), degraded=degraded, rewritten=rewritten)
        return GuidelineSearchResult(
            chunks=out, provider=provider, reranker=reranker_name, degraded=degraded
        )


@lru_cache
def get_index() -> HybridIndex:
    """Process-wide singleton, built lazily on first use. `reset_index()` (or
    `get_index.cache_clear()`) drops it, e.g. after a settings change in tests."""
    s = get_settings()
    return HybridIndex(
        chunks=load_corpus(),
        embedder=select_embedding(s),
        reranker=select_reranker(s),
        top_k=s.retrieval_top_k,
    )


def reset_index() -> None:
    get_index.cache_clear()


def vector_index_ready() -> bool:
    """Cheap /ready probe (W2 rubric): the index is buildable — corpus present
    and non-empty. Never triggers the lazy build (the whole point of laziness
    is that startup and health probes don't pay for it or crash on it)."""
    try:
        from . import corpus as corpus_mod

        d = corpus_mod.DEFAULT_CORPUS_DIR
        return d.is_dir() and any(d.glob("*.md"))
    except Exception:  # noqa: BLE001 — a probe never raises
        return False
