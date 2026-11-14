"""Hermetic tests for the handoff sinks (W2 D5, §7.3) — PHI discipline.

Failure modes guarded:
- (invariant/PHI) `handoff` log lines carry enum/ID fields ONLY — the free-text
  `reason`/`payload_summary` never reach the log sink, even though `reason`
  happens to be an allowlisted key for other events;
- (invariant) the response's handoff trail is complete, ordered, and carries
  the turn's correlation ID;
- (invariant/PHI) worker summaries are counts + handle ids by construction —
  no evidence values (lab numbers, drug names) can ride the supervisor
  transcript or the handoff payload.
"""

from __future__ import annotations

import json
import re

import pytest

from app.correlation import set_correlation_id
from app.tools import ToolContext

# Enum/ID keys a handoff log line may carry (plus formatter envelope fields).
_ALLOWED_HANDOFF_LINE_KEYS = {
    "ts", "level", "logger", "correlation_id", "msg",
    "route", "from_node", "to_node", "seq", "handoff_id", "latency_ms",
    "_dropped_fields",
}

_SUMMARY_SHAPE = re.compile(
    r"^(ok|degraded): \d+ evidence handle\(s\) registered(: [a-z]+:\d+(, [a-z]+:\d+)*)?$"
)


@pytest.fixture
def ctx():
    set_correlation_id("handoff-test-cid")
    return ToolContext("handoff-test-cid", 1, 101)


def _run_mock(ctx, user_text):
    from app.agent import run_turn
    from app.observability import TurnObserver

    obs = TurnObserver(101, "message")
    return run_turn(ctx, user_text, [], obs), obs


def test_handoff_log_lines_carry_only_allowlisted_fields(
    mock_llm_env, stub_tools, log_capture, ctx
):
    """Sink 1 (structured logs): enum/ID fields only — no reason/payload text."""
    _run_mock(ctx, "please review the uploaded intake form")

    lines = [json.loads(ln) for ln in log_capture.getvalue().splitlines() if ln.strip()]
    handoff_lines = [ln for ln in lines if ln.get("msg") == "handoff"]
    # route out, worker_return, finalize, critique, critic outcome
    assert len(handoff_lines) == 5

    for line in handoff_lines:
        assert set(line) <= _ALLOWED_HANDOFF_LINE_KEYS, f"unexpected keys: {set(line) - _ALLOWED_HANDOFF_LINE_KEYS}"
        assert "reason" not in line
        assert "payload_summary" not in line

    # The free-text reason/payload never appear anywhere in the log stream.
    full_log = log_capture.getvalue()
    assert "mock keyword routing" not in full_log
    assert "evidence handle(s) registered" not in full_log


def test_handoff_records_in_response_are_complete_and_ordered(
    mock_llm_env, stub_tools, ctx
):
    """Sink 3 (AgentResponse.handoffs): full records, correct seq/from/to/route,
    correlation ID threaded through every record."""
    resp, _ = _run_mock(ctx, "what does the uploaded document say about her labs?")

    assert [(h.from_node, h.to_node, h.route) for h in resp.handoffs] == [
        ("supervisor", "intake_extractor", "route_to_intake_extractor"),
        ("intake_extractor", "supervisor", "worker_return"),
        ("supervisor", "answerer", "finalize"),
        ("answerer", "critic", "critique"),
        ("critic", "answerer", "critic_passed"),
    ]
    assert [h.seq for h in resp.handoffs] == [0, 1, 2, 3, 4]
    for h in resp.handoffs:
        assert h.correlation_id == "handoff-test-cid"
        assert h.handoff_id  # non-empty unique id
    # worker_return carries the worker latency; the response keeps free text.
    assert resp.handoffs[1].latency_ms is not None
    assert resp.handoffs[1].reason  # free text allowed in the response sink


def test_worker_summary_is_counts_and_handles_only(mock_llm_env, stub_tools, log_capture, ctx):
    """PHI: the intake worker saw labs 8.7 % and 142 mmol/L; its summary (the
    only worker output that reaches the supervisor transcript and the handoff
    payload) must contain handle ids and counts ONLY — never those values."""
    resp, _ = _run_mock(ctx, "summarize the uploaded lab report pdf")

    worker_return = resp.handoffs[1]
    summary = worker_return.payload_summary
    assert _SUMMARY_SHAPE.match(summary), f"summary not counts+handles shaped: {summary!r}"
    assert "lab:0" in summary and "lab:1" in summary
    assert "8.7" not in summary
    assert "142" not in summary
    assert "Hemoglobin" not in summary and "Sodium" not in summary

    # And the log stream never saw the values or names either (drop the
    # timing/ts fields, which may legitimately contain arbitrary digits).
    for raw in log_capture.getvalue().splitlines():
        if not raw.strip():
            continue
        line = json.loads(raw)
        line.pop("ts", None)
        line.pop("latency_ms", None)
        line.pop("steps", None)
        text = json.dumps(line)
        assert "8.7" not in text
        assert "Hemoglobin" not in text and "Sodium" not in text and "lisinopril" not in text
