"""Hermetic tests for TurnObserver's per-node spans and aggregates (W2 D5).

Failure modes guarded:
- (invariant) spans carry node attribution and `node_ms` equals the sum of
  that node's step timings — the latency report and dashboard depend on it;
- (invariant) record_tokens(node=...) rolls up per-node token aggregates
  without disturbing the Week 1 totals;
- (regression) the Week 1 `step()` alias still works — main.py's /snapshot
  and /day paths call it unmodified.
"""

from __future__ import annotations

import time

from app.observability import TurnObserver


class _Usage:
    def __init__(self, tin, tout):
        self.input_tokens = tin
        self.output_tokens = tout


def test_span_node_attribution_and_node_ms_sums():
    obs = TurnObserver(101, "message")
    with obs.span("supervisor_0", node="supervisor"):
        time.sleep(0.002)
    with obs.span("llm_intake_0", node="intake_extractor"):
        time.sleep(0.002)
    with obs.span("tool_get_labs", node="intake_extractor"):
        time.sleep(0.002)

    assert [s["node"] for s in obs.steps] == ["supervisor", "intake_extractor", "intake_extractor"]
    assert all(set(s) == {"name", "node", "ms"} for s in obs.steps)

    # node_ms is exactly the rounded running sum of that node's step timings.
    sup_steps = [s["ms"] for s in obs.steps if s["node"] == "supervisor"]
    ie_steps = [s["ms"] for s in obs.steps if s["node"] == "intake_extractor"]
    assert obs.node_ms["supervisor"] == round(sum(sup_steps), 1)
    expected_ie = 0.0
    for ms in ie_steps:  # mirrors the incremental rounding
        expected_ie = round(expected_ie + ms, 1)
    assert obs.node_ms["intake_extractor"] == expected_ie


def test_record_tokens_per_node_and_totals():
    obs = TurnObserver(101, "message")
    obs.record_tokens(_Usage(100, 20), node="supervisor")
    obs.record_tokens(_Usage(50, 10), node="answerer")
    obs.record_tokens(_Usage(25, 5), node="answerer")
    obs.record_tokens(_Usage(7, 3))  # un-attributed (Week 1 call shape)

    assert obs.input_tokens == 182 and obs.output_tokens == 38  # totals unchanged
    assert obs.node_tokens["supervisor"] == {"input": 100, "output": 20}
    assert obs.node_tokens["answerer"] == {"input": 75, "output": 15}
    assert set(obs.node_tokens) == {"supervisor", "answerer"}


def test_week1_step_alias_still_works():
    obs = TurnObserver(101, "snapshot")
    with obs.step("snapshot"):
        pass
    assert obs.steps[0]["name"] == "snapshot"
    assert obs.steps[0]["node"] is None
    assert "snapshot" not in obs.node_ms  # no node attribution → no rollup


def test_metadata_exposes_node_aggregates_and_handoff_meta():
    obs = TurnObserver(101, "message")
    with obs.span("x", node="answerer"):
        pass
    obs.record_tokens(_Usage(10, 2), node="answerer")
    obs.record_cost(0.0031, node="answerer")
    obs.record_handoff({"seq": 0, "from_node": "supervisor", "to_node": "answerer",
                        "route": "finalize", "handoff_id": "abc", "latency_ms": 1.0})

    meta = obs.metadata()
    assert meta["node_ms"].keys() == {"answerer"}
    assert meta["node_tokens"]["answerer"] == {"input": 10, "output": 2}
    assert meta["node_cost"]["answerer"] == 0.0031
    assert meta["handoffs"][0]["route"] == "finalize"
    assert meta["cost_usd"] == 0.0031  # per-node cost rolls into the turn total
