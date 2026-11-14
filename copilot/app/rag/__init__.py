"""Hybrid RAG over the guideline corpus (Week 2, design decision D4).

Layout:
- `corpus.py`    — front-matter parse + deterministic heading-based chunker
- `bm25.py`      — sparse (lexical) leg, rank-bm25
- `providers.py` — EmbeddingProvider / Reranker Protocols + implementations
- `index.py`     — HybridIndex: BM25 ∪ dense → RRF fusion → rerank → top-k

Reference knowledge only — this package never touches patient data or the DB.
"""
