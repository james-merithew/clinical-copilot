"""Hermetic tests for `copilot/scripts/report_latency.py` (no server, no DB).

Each case pins a failure mode:
1. import trap — `load.py` mints tokens at module level (`get_signing_key()`
   hits the DB); the report harness must be importable with sockets blocked so
   the trap can never be reintroduced (measurement tooling must not need a
   live DB just to `--help`).
2. `pct` nearest-rank semantics — percentile math regressions would silently
   corrupt every published number.
3. `render_markdown` — the report must actually carry the per-flow p50/p95
   rows, the honesty mode label, and a bottleneck line naming the max-p95 node.
4. `analyze` ranking — the bottleneck analysis must rank nodes by p95, not by
   name/count, or it points at the wrong subsystem.
5. mode mismatch — claiming `--mode live` while the /chat body carries the
   mock signature must fail loudly (invariant: never publish numbers under a
   false label).
"""

from __future__ import annotations

import importlib.util
import math
import socket
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "report_latency.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("report_latency_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations via sys.modules[cls.__module__],
    # so the module must be registered before exec.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return mod


# --- 1. import must be hermetic (no DB, no network, no env) -------------------


def test_import_no_network_no_db(monkeypatch):
    """Importing the harness with sockets blocked must succeed: token minting
    (get_signing_key -> DB) is only allowed inside main(). Guards against the
    load.py module-level-minting trap."""

    def _blocked(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("network/DB access at import time")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    # No COPILOT_* env needed either: exec_module runs the file fresh here.
    mod = _load_module()
    # The API surface main() and the tests rely on exists.
    for fn in ("pct", "run_flow", "scrape_summary", "analyze",
               "render_markdown", "render_json", "main", "detect_mode"):
        assert hasattr(mod, fn), f"missing {fn}"
    # And nothing token-shaped was minted at import time.
    assert not hasattr(mod, "TOKENS")


# --- 2. pct nearest-rank ------------------------------------------------------


def test_pct_nearest_rank():
    mod = _load_module()
    vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    # Classic nearest-rank: idx = ceil(p/100 * n) - 1.
    assert mod.pct(vals, 50) == 50
    assert mod.pct(vals, 95) == 100
    assert mod.pct(vals, 99) == 100
    assert mod.pct(vals, 100) == 100
    assert mod.pct([7.5], 95) == 7.5
    # Unsorted input must not corrupt the result.
    assert mod.pct([30, 10, 20], 50) == 20
    assert math.isnan(mod.pct([], 50))


# --- shared canned fixtures -----------------------------------------------------


def _canned_flows(mod):
    def flow(name, lats, n=None, excluded=0, codes=None):
        return mod.FlowStats(
            name=name,
            requests=(n if n is not None else len(lats) + excluded),
            ok=len(lats),
            excluded=excluded,
            status_counts=codes or {"200": len(lats)},
            latencies_ms=list(lats),
        )

    return [
        flow("ready", [3.0, 4.0, 5.0, 6.0, 20.0]),
        flow("snapshot", [120.0, 130.0, 140.0, 150.0, 400.0]),
        flow("day", [200.0, 210.0, 220.0, 230.0, 600.0]),
        flow("chat_message", [3000.0, 3500.0, 4000.0, 4500.0, 9000.0],
             excluded=1, codes={"200": 5, "429": 1}),
        flow("chat_chip_care_gaps", [2800.0, 3200.0, 3600.0, 8000.0]),
    ]


def _canned_summary(turns_count=10):
    """Shape copied from app.metrics_summary.MetricsSummary.summary()."""
    return {
        "generated_ts": "2026-07-16T12:00:00",
        "uptime_s": 900.0,
        "endpoints": {
            "GET /ready": {"count": 25, "errors_4xx": 0, "errors_5xx": 0,
                           "avg_ms": 5.0, "p50_ms": 4.0, "p95_ms": 19.0},
            "GET /snapshot": {"count": 25, "errors_4xx": 0, "errors_5xx": 0,
                              "avg_ms": 160.0, "p50_ms": 135.0, "p95_ms": 390.0},
            "POST /chat": {"count": 11, "errors_4xx": 1, "errors_5xx": 0,
                           "avg_ms": 4200.0, "p50_ms": 3700.0, "p95_ms": 8800.0},
        },
        "turns": {
            "count": turns_count, "errors": 0, "degraded": 0,
            "verified_claims": 40, "quarantined_claims": 2,
            "by_kind": {"message": 5, "chip": 5},
            "avg_ms": 4100.0, "p50_ms": 3600.0, "p95_ms": 8500.0,
        },
        "nodes": {
            "answerer": {"count": 10, "avg_ms": 3000.0, "p50_ms": 2800.0, "p95_ms": 6800.0},
            "supervisor": {"count": 10, "avg_ms": 700.0, "p50_ms": 650.0, "p95_ms": 1400.0},
            "verifier": {"count": 10, "avg_ms": 20.0, "p50_ms": 18.0, "p95_ms": 45.0},
        },
        "routing": {"finalize": 8, "evidence": 2},
        "ingestion": {"count": 0, "persisted": 0, "deduplicated": 0, "needs_review": 0,
                      "failed": 0, "fields_total": 0, "fields_extracted": 0,
                      "fields_failed_verification": 0, "field_pass_rate": None},
        "retrieval": {"queries": 2, "hits": 2, "degraded": 0, "hit_rate": 1.0},
        "evals": None,
    }


def _meta(mode="mock"):
    return {
        "mode_claimed": mode,
        "mode_observed": mode,
        "mode_mismatch": False,
        "date": "2026-07-16",
        "url": "http://127.0.0.1:8808",
        "git_sha": "abc1234",
        "min_interval_s": 2.1,
        "n_per_flow": {"ready": 5, "snapshot": 5, "day": 5,
                       "chat_message": 6, "chat_chip_care_gaps": 4},
    }


# --- 3. render_markdown ---------------------------------------------------------


def test_render_markdown_contains_rows_mode_and_bottleneck():
    mod = _load_module()
    flows = _canned_flows(mod)
    before = _canned_summary(turns_count=0)
    after = _canned_summary(turns_count=10)
    md = mod.render_markdown(_meta("mock"), flows, before, after)

    # Mode label stamped in the header.
    assert "mock" in md
    # Per-flow wall-clock rows with p50/p95 present.
    for name in ("ready", "snapshot", "day", "chat_message", "chat_chip_care_gaps"):
        assert name in md
    assert "p50" in md and "p95" in md
    assert "4000.0" in md  # chat_message p50 (nearest-rank: 3rd of 5 values)
    assert "9000.0" in md  # chat_message p95 (5th of 5)
    # Server-side scrape rendered.
    assert "8500" in md    # turns p95 from /metrics/summary
    # Bottleneck line names the max-p95 node.
    assert "answerer" in md
    # Sliding-window + loopback caveats are part of the honesty header.
    assert "512" in md
    assert "loopback" in md.lower() or "co-located" in md.lower()

    # render_json carries the raw material: flows + both scrapes.
    doc = mod.render_json(_meta("mock"), flows, before, after)
    assert doc["meta"]["mode_claimed"] == "mock"
    assert doc["summary_before"]["turns"]["count"] == 0
    assert doc["summary_after"]["turns"]["count"] == 10
    names = {f["name"] for f in doc["flows"]}
    assert "chat_message" in names and "ready" in names


# --- 4. analyze ranks nodes by p95 ---------------------------------------------


def test_analyze_ranks_nodes_by_p95():
    mod = _load_module()
    flows = {f.name: f for f in _canned_flows(mod)}
    lines = mod.analyze(flows, _canned_summary())
    text = "\n".join(lines)
    # answerer (6800) > supervisor (1400) > verifier (45) — in that order.
    assert text.index("answerer") < text.index("supervisor") < text.index("verifier")
    # The share-of-turn line names the top node against turns.p95.
    share_line = next(ln for ln in lines if "answerer" in ln and "turn" in ln.lower())
    assert "6800" in share_line and "8500" in share_line
    # /snapshot-vs-/chat isolation and the saturation pointer are present.
    assert any("snapshot" in ln for ln in lines)
    assert any("RESULTS.md" in ln for ln in lines)
    # No nodes section -> no crash, still returns the pointer line.
    assert mod.analyze(flows, None)


# --- 5. mode-mismatch detection ---------------------------------------------------


def test_mode_mismatch_raises():
    mod = _load_module()
    mock_body = {"correlation_id": "c-1", "briefing": [],
                 "notes": "Mock LLM briefing (load-test mode)."}
    live_body = {"correlation_id": "c-2", "briefing": [], "notes": None}

    assert mod.detect_mode(mock_body) == "mock"
    assert mod.detect_mode(live_body) == "live"

    with pytest.raises(mod.ModeMismatch):
        mod.check_mode(claimed="live", observed="mock")
    with pytest.raises(mod.ModeMismatch):
        mod.check_mode(claimed="mock", observed="live")
    # Agreement passes silently.
    mod.check_mode(claimed="mock", observed="mock")
    mod.check_mode(claimed="live", observed="live")
