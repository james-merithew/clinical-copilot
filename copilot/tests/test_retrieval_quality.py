"""Aggregate retrieval-quality gate — recall@k + MRR floors over the labeled query set.

Closes the Week-2 "retrieval quality is measured" gap with a corpus-wide SCORED metric
(not the prior 3-query top-3 spot check) AND makes it a regression gate: if RRF fusion,
deterministic query expansion, or the corpus degrades retrieval, these floors fail in CI.
Metrics come from the SAME keyless index that ships without a Cohere key.

Floors sit below the current measured values (recall@1 0.79, recall@3 0.97, recall@5
1.00, MRR 0.90) with headroom so the gate catches real regressions without flaking; the
committed `evals/retrieval/metrics.json` records the exact numbers and is drift-checked.

Failure modes guarded:
- a fusion/expansion regression silently lowering retrieval quality (floors);
- the committed metrics record going stale vs. the actual measurement (drift);
- a labeled-set that stops covering every corpus doc (coverage) — a metric that
  doesn't exercise a doc can't catch that doc regressing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_COPILOT = Path(__file__).resolve().parents[1]
MEASURE_PATH = _COPILOT / "evals" / "retrieval" / "measure.py"
METRICS_PATH = _COPILOT / "evals" / "retrieval" / "metrics.json"

# Regression floors — below the measured values, with headroom.
FLOORS = {"recall@1": 0.70, "recall@3": 0.90, "recall@5": 0.95, "mrr": 0.80}


def _load_measure():
    spec = importlib.util.spec_from_file_location("retrieval_measure", MEASURE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["retrieval_measure"] = mod
    spec.loader.exec_module(mod)
    return mod


measure = _load_measure()
_METRICS = measure.compute_metrics()


def test_recall_and_mrr_meet_floors():
    for metric, floor in FLOORS.items():
        assert _METRICS[metric] >= floor, f"{metric}={_METRICS[metric]} below floor {floor}"


def test_committed_metrics_are_not_stale():
    """The committed record must match a fresh measurement (retrieval is deterministic)."""
    committed = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    for metric in ("recall@1", "recall@3", "recall@5", "mrr"):
        assert committed[metric] == _METRICS[metric], (
            f"{metric}: committed {committed[metric]} != measured {_METRICS[metric]} — "
            "re-run measure.py and commit metrics.json"
        )


def test_every_corpus_doc_is_labeled():
    """Coverage: each of the 9 corpus docs is the relevant target of >=1 labeled query,
    so the aggregate metric actually exercises every doc."""
    from app.rag.corpus import load_corpus

    corpus_docs = {c.doc_id for c in load_corpus()}
    labeled = {d for item in measure.load_labels() for d in item["relevant"]}
    missing = corpus_docs - labeled
    assert not missing, f"corpus docs with no labeled query: {sorted(missing)}"
