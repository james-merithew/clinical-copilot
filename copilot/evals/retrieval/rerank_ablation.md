# Reranker ablation — contribution over the keyless first stage

17 labeled queries; first stage: BM25 + hashed TF-IDF + RRF (keyless). Metrics are the reranker-sensitive ones (recall@1, MRR); recall@5 is first-stage-bound.

| reranker | recall@1 | recall@3 | MRR |
| --- | --- | --- | --- |
| OFF (noop, RRF order) | 0.7941 | 0.9706 | 0.9020 |
| ON (lexical, keyless) | 0.6471 | 0.8235 | 0.7598 |
| ORACLE (upper bound) | 1.0000 | 1.0000 | 1.0000 |
| LIVE (no key) | n/a | n/a | n/a |

- **Reranking headroom (MRR):** +0.0980 — the most any reranker of this pool can add over the RRF baseline.
- **Keyless lexical reranker delta (MRR):** -0.1422 — a real, no-key reranking method, so the contribution is measured even without a provider key.
- **LIVE row:** run with `COPILOT_RERANK_PROVIDER=auto` + a Cohere/Anthropic key to record the shipping reranker's captured fraction of the headroom.
