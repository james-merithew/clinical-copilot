"""Embedding + reranking providers behind typed Protocols (W2 §6.2).

The fallback ladder is the point: retrieval must keep working with zero keys.

    embed:  Cohere embed-v4.0 (5s, 1 retry)   →  hashed TF-IDF (pure python)
    rerank: Cohere rerank-v3.5 (3s, no retry)  →  Claude listwise (3s budget)
                                               →  NoopReranker (RRF order stands)

Provider selection ("auto"): Cohere iff a key is present, else local.
`mock_llm` mode pins local-tfidf + noop so tests and k6 runs are hermetic.
All network SDK imports are lazy — a keyless environment never imports them.
"""

from __future__ import annotations

import math
import time
import zlib
from collections import Counter
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .bm25 import tokenize

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

COHERE_EMBED_MODEL = "embed-v4.0"
COHERE_RERANK_MODEL = "rerank-v3.5"
EMBED_TIMEOUT_S = 5.0
RERANK_TIMEOUT_S = 3.0
# Embed retry budget = 1 retry (2 attempts). W2_ARCHITECTURE §9.2: "Cohere
# embed | 5s | 1". Each attempt is bounded by the per-request SDK timeout
# (`timeout=EMBED_TIMEOUT_S`, applied in `_cli`), so a mid-turn embed outage
# costs at most ~2×5s = 10s — well under the 20s turn deadline — before it
# degrades to BM25-only. (The rerank path is 0 retries, `_retrying(call, 0)`.)
MAX_RETRIES = 1  # bounded retries on transient provider errors
_COHERE_EMBED_BATCH = 96  # API max texts per call


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str

    def fit(self, texts: list[str]) -> None:  # corpus-level stats (no-op for APIs)
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class Reranker(Protocol):
    name: str

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        """Return indices into `documents`, most→least relevant."""
        ...


def _retrying(fn, retries: int):  # noqa: ANN001 — tiny local helper
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — provider errors degrade, never raise types
            last = exc
    raise last  # type: ignore[misc]


# ---- Local, deterministic, keyless -------------------------------------------


class LocalTfidfEmbedding:
    """Hashed TF-IDF + cosine — pure python, deterministic, zero keys.

    Tokens hash (crc32 — NOT builtin hash(), which is salted per process) into
    a fixed-dimension bag; weights are tf·idf, L2-normalized so dot product is
    cosine similarity."""

    name = "tfidf"

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self._idf: dict[str, float] = {}

    def fit(self, texts: list[str]) -> None:
        n = len(texts)
        df: Counter[str] = Counter()
        for t in texts:
            df.update(set(tokenize(t)))
        self._idf = {tok: math.log((1 + n) / (1 + d)) + 1.0 for tok, d in df.items()}

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok, tf in Counter(tokenize(text)).items():
            idf = self._idf.get(tok)
            if idf is None:
                # Out-of-vocabulary tokens (once fitted) carry no meaning in a
                # hashed space — mapping them would only add collision noise.
                if self._idf:
                    continue
                idf = 1.0
            v[zlib.crc32(tok.encode("utf-8")) % self.dim] += tf * idf
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v] if norm else v

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class NoopReranker:
    """Last resort: the RRF fusion order stands."""

    name = "noop"

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        return list(range(min(top_n, len(documents))))


# ---- Cohere (primary when a key is present) -----------------------------------


class CohereEmbedding:
    name = "cohere"

    def __init__(self, api_key: str, timeout_s: float = EMBED_TIMEOUT_S) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = None

    def _cli(self):  # noqa: ANN202 — lazy import so keyless envs never load the SDK
        if self._client is None:
            import cohere

            self._client = cohere.ClientV2(api_key=self._api_key, timeout=self._timeout_s)
        return self._client

    def fit(self, texts: list[str]) -> None:  # noqa: ARG002 — API model needs no corpus stats
        return None

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), _COHERE_EMBED_BATCH):
            batch = texts[i : i + _COHERE_EMBED_BATCH]

            def call():  # noqa: ANN202
                resp = self._cli().embed(
                    texts=batch,
                    model=COHERE_EMBED_MODEL,
                    input_type=input_type,
                    embedding_types=["float"],
                )
                emb = resp.embeddings
                vecs = getattr(emb, "float_", None) or getattr(emb, "float", None)
                if vecs is None:
                    raise ValueError("cohere embed returned no float embeddings")
                return [list(v) for v in vecs]

            out.extend(_retrying(call, MAX_RETRIES))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "search_document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "search_query")[0]


class CohereReranker:
    name = "cohere"

    def __init__(self, api_key: str, timeout_s: float = RERANK_TIMEOUT_S) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = None

    def _cli(self):  # noqa: ANN202
        if self._client is None:
            import cohere

            self._client = cohere.ClientV2(api_key=self._api_key, timeout=self._timeout_s)
        return self._client

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        def call() -> list[int]:
            resp = self._cli().rerank(
                model=COHERE_RERANK_MODEL, query=query, documents=documents, top_n=top_n
            )
            return [r.index for r in resp.results]

        # No retries (W2_ARCHITECTURE §9.2): a late rerank is worth less than
        # the RRF order it delays — one 3s attempt, then the fallback ladder.
        return _retrying(call, 0)


# ---- Claude listwise (keyless-Cohere fallback when an Anthropic key exists) ----

_RANK_TOOL = {
    "name": "rank_passages",
    "description": "Return ALL passage indices ordered from most to least relevant to the query.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ranking": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "passage indices, most relevant first",
            }
        },
        "required": ["ranking"],
    },
}


class ClaudeListwiseReranker:
    """One Anthropic call with a forced `rank_passages` tool (3s budget, no SDK
    retries — a slow rerank is worth less than the RRF order it would delay)."""

    name = "claude"

    def __init__(self, api_key: str, model: str, timeout_s: float = RERANK_TIMEOUT_S) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        import anthropic

        client = anthropic.Anthropic(
            api_key=self._api_key, timeout=self._timeout_s, max_retries=0
        )
        passages = "\n".join(f"[{i}] {d[:400]}" for i, d in enumerate(documents))
        deadline = time.perf_counter() + self._timeout_s
        resp = client.messages.create(
            model=self._model,
            max_tokens=256,
            tools=[_RANK_TOOL],
            tool_choice={"type": "tool", "name": "rank_passages"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Rank these clinical-guideline passages by relevance to the query.\n"
                        f"Query: {query}\nPassages:\n{passages}\n"
                        "Call rank_passages with every index, most relevant first."
                    ),
                }
            ],
        )
        if time.perf_counter() > deadline:
            raise TimeoutError("listwise rerank exceeded budget")
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "rank_passages":
                raw = block.input.get("ranking", [])
                seen: set[int] = set()
                order = []
                for i in raw:
                    if isinstance(i, int) and 0 <= i < len(documents) and i not in seen:
                        seen.add(i)
                        order.append(i)
                order.extend(i for i in range(len(documents)) if i not in seen)
                return order[:top_n]
        raise ValueError("rank_passages tool_use missing from response")


# ---- Selection ("auto" ladder, W2 §6.2) ----------------------------------------


def select_embedding(settings: "Settings") -> EmbeddingProvider:
    if settings.mock_llm:
        return LocalTfidfEmbedding()
    mode = (settings.embed_provider or "auto").lower()
    if mode == "cohere":
        return CohereEmbedding(settings.cohere_api_key)
    if mode in ("local", "tfidf", "local-tfidf"):
        return LocalTfidfEmbedding()
    # auto: Cohere iff a key is present
    if settings.cohere_api_key:
        return CohereEmbedding(settings.cohere_api_key)
    return LocalTfidfEmbedding()


def reranker_ready(settings: "Settings") -> bool:
    """Cheap /ready probe (W2 rubric): is a real reranker usable? Config-only —
    never a network or paid call. Noop fallback counts as NOT ready: retrieval
    still works (RRF order stands) but that is the degraded mode, not health."""
    try:
        if settings.mock_llm:
            return True
        reranker = select_reranker(settings)
        return not isinstance(reranker, NoopReranker)
    except Exception:  # noqa: BLE001 — a probe never raises
        return False


def select_reranker(settings: "Settings") -> Reranker:
    if settings.mock_llm:
        return NoopReranker()
    mode = (settings.rerank_provider or "auto").lower()
    if mode == "cohere":
        return CohereReranker(settings.cohere_api_key)
    if mode == "claude":
        return ClaudeListwiseReranker(settings.anthropic_api_key, settings.anthropic_model)
    if mode in ("noop", "none"):
        return NoopReranker()
    # auto ladder: Cohere → Claude listwise → Noop
    if settings.cohere_api_key:
        return CohereReranker(settings.cohere_api_key)
    if settings.anthropic_api_key:
        return ClaudeListwiseReranker(settings.anthropic_api_key, settings.anthropic_model)
    return NoopReranker()
