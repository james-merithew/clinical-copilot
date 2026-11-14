"""Hermetic tests for provider selection ("auto" ladder) and the local
deterministic embedding. No network: Cohere/Anthropic classes are only ever
CONSTRUCTED (their SDK imports are lazy, at call time).

Failure modes guarded:
- "auto" silently picking a networked provider in a keyless/hermetic env
  (tests, k6, the eval gate) would make the suite flaky and key-dependent;
- mock_llm mode reaching a real provider would leak tokens in load tests;
- a non-deterministic local embedding would break retrieval reproducibility.
"""

from __future__ import annotations

import pytest

from app import rag as _rag_pkg  # noqa: F401 — ensures app.rag importable
from app.config import Settings
from app.rag import providers
from app.rag.providers import (
    ClaudeListwiseReranker,
    CohereEmbedding,
    CohereReranker,
    LocalTfidfEmbedding,
    NoopReranker,
    select_embedding,
    select_reranker,
)


def make_settings(**kw) -> Settings:
    """Settings isolated from the developer's real .env / environment: every
    provider-relevant field is passed explicitly (init kwargs win)."""
    base = dict(
        _env_file=None,
        cohere_api_key="",
        anthropic_api_key="",
        mock_llm=False,
        embed_provider="auto",
        rerank_provider="auto",
    )
    base.update(kw)
    return Settings(**base)


def test_auto_keyless_selects_local_tfidf_and_noop():
    """INVARIANT (keyless fallback): no keys → pure-local providers, so the
    hermetic suite and a keyless deploy still retrieve."""
    s = make_settings()
    assert isinstance(select_embedding(s), LocalTfidfEmbedding)
    assert isinstance(select_reranker(s), NoopReranker)


def test_auto_with_cohere_key_selects_cohere():
    s = make_settings(cohere_api_key="ck-test")
    assert isinstance(select_embedding(s), CohereEmbedding)
    assert isinstance(select_reranker(s), CohereReranker)


def test_auto_anthropic_only_uses_claude_listwise_rerank():
    """LADDER: no Cohere key but an Anthropic key → local embeddings + Claude
    listwise reranker (the middle rung)."""
    s = make_settings(anthropic_api_key="sk-test")
    assert isinstance(select_embedding(s), LocalTfidfEmbedding)
    assert isinstance(select_reranker(s), ClaudeListwiseReranker)


def test_mock_llm_forces_local_even_with_keys():
    """INVARIANT (hermetic mock mode): mock_llm pins tfidf+noop regardless of
    keys — load tests must never spend tokens or hit the network."""
    s = make_settings(cohere_api_key="ck", anthropic_api_key="sk", mock_llm=True)
    assert isinstance(select_embedding(s), LocalTfidfEmbedding)
    assert isinstance(select_reranker(s), NoopReranker)


def test_explicit_provider_pins_override_auto():
    s = make_settings(cohere_api_key="ck", embed_provider="local", rerank_provider="noop")
    assert isinstance(select_embedding(s), LocalTfidfEmbedding)
    assert isinstance(select_reranker(s), NoopReranker)


def test_local_tfidf_is_deterministic_and_normalized():
    """REGRESSION: same corpus + text → identical vectors across instances
    (crc32 hashing, not salted builtin hash), L2-normalized."""
    docs = ["metformin dose egfr", "blood pressure target adults", "statin therapy risk"]
    a, b = LocalTfidfEmbedding(), LocalTfidfEmbedding()
    a.fit(docs)
    b.fit(docs)
    va = a.embed_query("metformin egfr")
    vb = b.embed_query("metformin egfr")
    assert va == vb
    assert abs(sum(x * x for x in va) - 1.0) < 1e-9


def test_local_tfidf_oov_query_is_zero_vector():
    """BOUNDARY: a fitted embedder maps out-of-vocabulary queries to the zero
    vector (no hash-collision noise) — the dense leg cleanly abstains."""
    e = LocalTfidfEmbedding()
    e.fit(["metformin dose egfr"])
    assert all(x == 0.0 for x in e.embed_query("zzzz qqqq"))


def test_noop_reranker_preserves_order_and_caps_top_n():
    assert NoopReranker().rerank("q", ["a", "b", "c"], 2) == [0, 1]


def test_cohere_embed_retry_budget_is_one_retry(monkeypatch):
    """REGRESSION (W2 §9.2, line 512 "Cohere embed | 5s | 1"): the embed retry
    budget must be 1 retry = 2 attempts, not 2 retries (3 attempts, ~15s). Inside
    a 20s turn deadline a 3-attempt embed outage (~15s) forces a thin briefing;
    2 attempts (~10s) stay well under budget. Asserts the CONSTANT and the actual
    attempt count (a fake client that always fails is called exactly twice)."""
    assert providers.MAX_RETRIES == 1

    attempts = {"n": 0}

    class _AlwaysFails:
        def embed(self, **kw):  # noqa: ANN003
            attempts["n"] += 1
            raise RuntimeError("cohere 5xx")

    emb = CohereEmbedding(api_key="ck-test")
    monkeypatch.setattr(emb, "_cli", lambda: _AlwaysFails())
    with pytest.raises(RuntimeError):
        emb.embed_query("metformin egfr cutoff")
    assert attempts["n"] == 2  # 1 initial attempt + 1 retry
