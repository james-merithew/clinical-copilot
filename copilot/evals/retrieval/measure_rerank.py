#!/usr/bin/env python
"""Reranker ablation — measure the reranking stage's contribution to grounding.

Closes the Week-2 "reranker's contribution is measured" gap: the reranker existed
(Cohere rerank-v3.5 / Claude-listwise) but was pinned to Noop in eval, so its
contribution was never quantified. This measures reranking ON vs OFF over the labeled
query set (the same qrels as measure.py), reporting the reranker-sensitive metrics —
recall@1 and MRR (a reranker reorders the RRF pool; it can't add a doc the pool lacks,
so recall@5 is first-stage-bound and not the interesting signal here).

Rows measured, all against the identical BM25+TF-IDF first stage:
  - OFF (noop)      : RRF order stands — the keyless shipping baseline;
  - ON  (lexical)   : a deterministic, KEYLESS query-term-overlap reranker — a real
                      reranking method, so the harness demonstrably measures a delta with
                      zero keys/network;
  - ORACLE (bound)  : the best any reranker of this pool could do (relevant doc promoted
                      to rank 1 whenever the first stage retrieved it) — the HEADROOM;
  - LIVE (provider) : the shipping reranker (Cohere/Claude) via COPILOT_RERANK_PROVIDER +
                      a key — filled by an operator run; keyless runs record it as n/a.

Run:  python copilot/evals/retrieval/measure_rerank.py
Enforced (harness sanity + upper-bound ordering) in tests/test_rerank_ablation.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

COPILOT_DIR = Path(__file__).resolve().parents[2]
if str(COPILOT_DIR) not in sys.path:
    sys.path.insert(0, str(COPILOT_DIR))

HERE = Path(__file__).resolve().parent


def _measure_mod():
    spec = importlib.util.spec_from_file_location("retrieval_measure_for_rerank", HERE / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["retrieval_measure_for_rerank"] = mod
    spec.loader.exec_module(mod)
    return mod


measure = _measure_mod()


class LexicalRerank:
    """Keyless, deterministic reranker: score each candidate by how many DISTINCT query
    terms it contains, ties broken by the incoming (RRF) order. A genuine reranking
    function — no key, no network — so the ablation shows a real, reproducible delta."""

    name = "lexical-keyless"

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        scored = []
        for i, doc in enumerate(documents):
            low = doc.lower()
            score = sum(1 for t in terms if t in low)
            scored.append((score, -i, i))  # -i keeps RRF order on ties
        scored.sort(reverse=True)
        return [i for _, _, i in scored[:top_n]]


def _index(reranker):
    from app.rag.corpus import load_corpus
    from app.rag.index import HybridIndex
    from app.rag.providers import LocalTfidfEmbedding

    return HybridIndex(load_corpus(), LocalTfidfEmbedding(), reranker, top_k=9)


def _metrics(reranker) -> dict:
    m = measure.compute_metrics(_index(reranker))
    return {"recall@1": m["recall@1"], "recall@3": m["recall@3"], "mrr": m["mrr"]}


def oracle_bound() -> dict:
    """Upper bound: a perfect reranker promotes a relevant doc to rank 1 whenever the
    first stage retrieved one — so the reranker-sensitive metrics equal the rate at which
    a relevant doc is anywhere in the pool."""
    from app.rag.providers import NoopReranker

    idx = _index(NoopReranker())
    labels = measure.load_labels()
    hit = 0
    for item in labels:
        pool = set(measure.ranked_doc_ids(idx, item["query"]))
        if set(item["relevant"]) & pool:
            hit += 1
    rate = round(hit / len(labels), 4)
    return {"recall@1": rate, "recall@3": rate, "mrr": rate}


def live_provider_metrics() -> dict | None:
    """The shipping reranker via the auto ladder — only if a key resolves it to a real
    provider (never Noop). Keyless runs return None (recorded as n/a)."""
    from app.config import get_settings
    from app.rag.providers import NoopReranker, select_reranker

    reranker = select_reranker(get_settings())
    if isinstance(reranker, NoopReranker) or getattr(reranker, "name", "noop") == "noop":
        return None
    return {"provider": reranker.name, **_metrics(reranker)}


def run() -> dict:
    from app.rag.providers import NoopReranker

    off = _metrics(NoopReranker())
    lexical = _metrics(LexicalRerank())
    oracle = oracle_bound()
    live = live_provider_metrics()
    return {
        "n_queries": len(measure.load_labels()),
        "first_stage": "BM25 + hashed TF-IDF + RRF (keyless)",
        "off_noop": off,
        "on_lexical_keyless": lexical,
        "oracle_upper_bound": oracle,
        "live_provider": live,  # None on keyless runs
        "headroom_mrr": round(oracle["mrr"] - off["mrr"], 4),
        "lexical_delta_mrr": round(lexical["mrr"] - off["mrr"], 4),
    }


def render_md(a: dict) -> str:
    def row(name, m):
        if not m:
            return f"| {name} | n/a | n/a | n/a |"
        return f"| {name} | {m['recall@1']:.4f} | {m['recall@3']:.4f} | {m['mrr']:.4f} |"

    lines = [
        "# Reranker ablation — contribution over the keyless first stage",
        "",
        f"{a['n_queries']} labeled queries; first stage: {a['first_stage']}. Metrics are the "
        "reranker-sensitive ones (recall@1, MRR); recall@5 is first-stage-bound.",
        "",
        "| reranker | recall@1 | recall@3 | MRR |",
        "| --- | --- | --- | --- |",
        row("OFF (noop, RRF order)", a["off_noop"]),
        row("ON (lexical, keyless)", a["on_lexical_keyless"]),
        row("ORACLE (upper bound)", a["oracle_upper_bound"]),
        row(f"LIVE ({(a['live_provider'] or {}).get('provider', 'no key')})", a["live_provider"]),
        "",
        f"- **Reranking headroom (MRR):** {a['headroom_mrr']:+.4f} — the most any reranker of "
        "this pool can add over the RRF baseline.",
        f"- **Keyless lexical reranker delta (MRR):** {a['lexical_delta_mrr']:+.4f} — a real, "
        "no-key reranking method, so the contribution is measured even without a provider key.",
        "- **LIVE row:** run with `COPILOT_RERANK_PROVIDER=auto` + a Cohere/Anthropic key to "
        "record the shipping reranker's captured fraction of the headroom.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    a = run()
    (HERE / "rerank_ablation.json").write_text(json.dumps(a, indent=2) + "\n", encoding="utf-8")
    (HERE / "rerank_ablation.md").write_text(render_md(a), encoding="utf-8")
    live = a["live_provider"]
    print(
        f"rerank ablation (n={a['n_queries']}): OFF MRR={a['off_noop']['mrr']:.3f}  "
        f"ON(lexical) MRR={a['on_lexical_keyless']['mrr']:.3f} (d={a['lexical_delta_mrr']:+.3f})  "
        f"ORACLE MRR={a['oracle_upper_bound']['mrr']:.3f} (headroom {a['headroom_mrr']:+.3f})  "
        f"LIVE={'n/a (keyless)' if not live else live['provider']}"
    )
    print(f"wrote {HERE / 'rerank_ablation.json'} and {HERE / 'rerank_ablation.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
