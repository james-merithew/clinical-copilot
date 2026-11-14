#!/usr/bin/env python
"""Aggregate retrieval-quality measurement — recall@k and MRR over a labeled query set.

Closes the Week-2 "retrieval quality is measured" gap: the repo had a 3-query top-3 spot
check, not a corpus-wide scored metric. This computes standard IR metrics over
`labeled_queries.json` (relevance judgments for all 9 guideline docs) against the SAME
keyless hybrid index that ships without a Cohere key (BM25 + hashed TF-IDF + noop rerank),
so the number is the real deployed retrieval quality, not a keyed best case.

Metrics (a query's ranked doc list = its chunk doc_ids, de-duplicated, order preserved):
  - recall@k  = mean over queries of |relevant ∩ top-k| / |relevant|
  - MRR       = mean over queries of 1 / (rank of the first relevant doc), 0 if none

Run:  python copilot/evals/retrieval/measure.py     # prints + writes metrics.{json,md}
The floors are enforced as a regression gate in tests/test_retrieval_quality.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

COPILOT_DIR = Path(__file__).resolve().parents[2]
if str(COPILOT_DIR) not in sys.path:
    sys.path.insert(0, str(COPILOT_DIR))

HERE = Path(__file__).resolve().parent
LABELS_PATH = HERE / "labeled_queries.json"
KS = (1, 3, 5)


def load_labels() -> list[dict]:
    data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    return data["queries"]


def build_keyless_index():
    """The exact keyless configuration that ships without a Cohere key."""
    from app.rag.corpus import load_corpus
    from app.rag.index import HybridIndex
    from app.rag.providers import LocalTfidfEmbedding, NoopReranker

    # top_k across the whole corpus so MRR sees the full ranking, not a 5-cap.
    return HybridIndex(load_corpus(), LocalTfidfEmbedding(), NoopReranker(), top_k=9)


def ranked_doc_ids(index, query: str) -> list[str]:
    seen: list[str] = []
    for chunk in index.search(query, top_k=9).chunks:
        did = chunk.ref.doc_id
        if did not in seen:
            seen.append(did)
    return seen


def compute_metrics(index=None) -> dict:
    index = index or build_keyless_index()
    labels = load_labels()
    recall_at = {k: 0.0 for k in KS}
    rr_sum = 0.0
    per_query = []

    for item in labels:
        query, relevant = item["query"], set(item["relevant"])
        ranked = ranked_doc_ids(index, query)
        rec = {k: len(relevant & set(ranked[:k])) / len(relevant) for k in KS}
        first_rank = next((i + 1 for i, d in enumerate(ranked) if d in relevant), None)
        rr = 1.0 / first_rank if first_rank else 0.0
        for k in KS:
            recall_at[k] += rec[k]
        rr_sum += rr
        per_query.append({
            "query": query, "relevant": sorted(relevant),
            "first_relevant_rank": first_rank,
            **{f"recall@{k}": round(rec[k], 4) for k in KS},
        })

    n = len(labels)
    return {
        "n_queries": n,
        "index": "keyless (BM25 + hashed TF-IDF + noop rerank)",
        "recall@1": round(recall_at[1] / n, 4),
        "recall@3": round(recall_at[3] / n, 4),
        "recall@5": round(recall_at[5] / n, 4),
        "mrr": round(rr_sum / n, 4),
        "per_query": per_query,
    }


def render_md(m: dict) -> str:
    lines = [
        "# Retrieval quality — aggregate recall@k + MRR",
        "",
        f"Keyless hybrid index (the no-Cohere-key shipping config), {m['n_queries']} labeled queries "
        "over the 9-doc guideline corpus.",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| recall@1 | {m['recall@1']:.4f} |",
        f"| recall@3 | {m['recall@3']:.4f} |",
        f"| recall@5 | {m['recall@5']:.4f} |",
        f"| MRR | {m['mrr']:.4f} |",
        "",
        "| query | relevant | first-rank | r@1 | r@3 | r@5 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for q in m["per_query"]:
        lines.append(
            f"| {q['query']} | {', '.join(q['relevant'])} | {q['first_relevant_rank']} "
            f"| {q['recall@1']:.2f} | {q['recall@3']:.2f} | {q['recall@5']:.2f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    m = compute_metrics()
    (HERE / "metrics.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    (HERE / "metrics.md").write_text(render_md(m), encoding="utf-8")
    print(
        f"retrieval quality (keyless, n={m['n_queries']}): "
        f"recall@1={m['recall@1']:.3f} recall@3={m['recall@3']:.3f} "
        f"recall@5={m['recall@5']:.3f} MRR={m['mrr']:.3f}"
    )
    print(f"wrote {HERE / 'metrics.json'} and {HERE / 'metrics.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
