"""Sparse (lexical) retrieval leg — BM25 over token-normalized chunks.

Sparse catches exact clinical tokens ("eGFR 30", "A1c") that dense embeddings
blur; the hybrid index fuses this leg with the dense leg via RRF (W2 §6.2).
"""

from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

from .corpus import Chunk

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def tokenize(text: str) -> list[str]:
    """Lowercased alphanumeric tokens; decimals kept whole ('7.5', 'a1c')."""
    return _TOKEN.findall((text or "").lower())


class Bm25Index:
    """BM25 over chunk text + section heading + doc title (headings carry the
    clinical topic words a short chunk body may lack)."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self._n = len(chunks)
        self._bm25 = (
            BM25Okapi([tokenize(f"{c.title} {c.section} {c.text}") for c in chunks])
            if chunks
            else None
        )

    def top(self, query: str, n: int) -> list[tuple[int, float]]:
        """Top-n (chunk_index, score) pairs, positive scores only, ties broken
        by index for determinism."""
        if not self._n:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        order = sorted(range(self._n), key=lambda i: (-scores[i], i))
        return [(i, float(scores[i])) for i in order[:n] if scores[i] > 0]
