"""Reranker ablation harness — hermetic checks for the contribution measurement.

Closes the 'reranker's contribution is measured' gap by proving the ablation harness is
real: it computes reranker-sensitive metrics (recall@1, MRR) for OFF (noop), a keyless
ON method, and an ORACLE upper bound, over the SAME first stage. These tests call the
harness's deterministic sub-functions directly — never `run()`, whose live-provider probe
could make a network call — so they stay fully hermetic (no key, no network).

Failure modes guarded:
- the harness silently degrading to a no-op measurement (off must equal the keyless
  retrieval baseline; oracle must be a real upper bound >= off);
- non-determinism in the measurement (re-run must match);
- the committed rerank_ablation.json drifting from a fresh keyless computation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_COPILOT = Path(__file__).resolve().parents[1]
RETRIEVAL_DIR = _COPILOT / "evals" / "retrieval"
ABLATION_JSON = RETRIEVAL_DIR / "rerank_ablation.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load("measure_rerank_under_test", RETRIEVAL_DIR / "measure_rerank.py")
retr = _load("retrieval_measure_under_test", RETRIEVAL_DIR / "measure.py")


def _keyless_rows():
    from app.rag.providers import NoopReranker

    return {
        "off_noop": rr._metrics(NoopReranker()),
        "on_lexical_keyless": rr._metrics(rr.LexicalRerank()),
        "oracle_upper_bound": rr.oracle_bound(),
    }


def test_off_equals_the_keyless_retrieval_baseline():
    """OFF (noop) reranking IS the first-stage baseline — the ablation shares the exact
    keyless index measure.py scores, so the numbers are commensurable."""
    off = rr._metrics(__import__("app.rag.providers", fromlist=["NoopReranker"]).NoopReranker())
    base = retr.compute_metrics()
    assert off["recall@1"] == base["recall@1"]
    assert off["mrr"] == base["mrr"]


def test_oracle_is_a_real_upper_bound():
    rows = _keyless_rows()
    off, oracle = rows["off_noop"], rows["oracle_upper_bound"]
    assert oracle["mrr"] >= off["mrr"], (oracle, off)
    assert oracle["recall@1"] >= off["recall@1"]
    # there is genuine headroom to measure against (the corpus isn't trivially rank-1)
    assert oracle["mrr"] > off["mrr"]


def test_measurement_is_deterministic():
    assert _keyless_rows() == _keyless_rows()


def test_committed_ablation_matches_fresh_keyless_run():
    """Drift guard: the committed record's deterministic rows match a fresh keyless
    computation (the live_provider row is env-dependent and deliberately not checked)."""
    committed = json.loads(ABLATION_JSON.read_text(encoding="utf-8"))
    fresh = _keyless_rows()
    for key in ("off_noop", "on_lexical_keyless", "oracle_upper_bound"):
        assert committed[key] == fresh[key], f"{key}: committed {committed[key]} != fresh {fresh[key]}"
    assert committed["live_provider"] is None, "committed artifact must be the keyless (CI-reproducible) one"
