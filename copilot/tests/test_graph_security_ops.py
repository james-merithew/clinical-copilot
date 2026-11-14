"""Security / PHI / graceful-degradation regressions in the graph answerer loop.

Failure modes guarded (adversarial review, Week 2):
- (invariant 1+8) a model-chosen tool name is untrusted: an injected/hallucinated
  PHI-bearing tool name must never reach a span name, a structured log line, or
  the LangSmith-bound observer metadata;
- (invariant 9) the Anthropic client must be constructed with a bounded per-turn
  timeout — the SDK's 600s default would let one slow call overrun the turn
  deadline by minutes;
- (invariant 9) once the wall-clock deadline is blown the answerer must stop
  starting new model calls (force-finalize) rather than looping/hanging;
- (invariant 9) a forced/cut-off finalize that yields an EMPTY briefing must be
  flagged degraded with a note, never a silent empty success.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.correlation import set_correlation_id
from app.observability import TurnObserver
from app.tools import ToolContext
from graph_utils import fake_resp, submit_briefing_resp, tool_use


def _answerer(ctx_cid="sec-cid", user_text="brief me", deadline=None):
    from app.graph.workers import _Turn, answerer

    set_correlation_id(ctx_cid)
    obs = TurnObserver(101, "message")
    ctx = ToolContext(ctx_cid, 1, 101)
    resp = answerer(ctx, user_text, [], _Turn(), obs, deadline=deadline)
    return resp, obs


# --- Finding 2: model-controlled tool name must not carry PHI into logs/telemetry


def test_answerer_phi_tool_name_never_reaches_logs_or_metadata(
    real_llm_env, stub_tools, fake_client_factory, log_capture
):
    """The answerer takes tu.name from the untrusted planner. A hallucinated,
    PHI-bearing tool name must not appear verbatim in the tool span, the
    tool_error log, or the observer metadata (which reaches LangSmith)."""
    phi = "get_labs_for_John_Q_Smith_warfarin"
    fake_client_factory([
        fake_resp(tool_use(phi, {"q": "x"}, id="tu_phi")),
        submit_briefing_resp(),
    ])

    resp, obs = _answerer("phi-toolname-cid")

    # It completed gracefully (KeyError tool_result went back to the model).
    assert resp is not None
    logs = log_capture.getvalue()
    assert phi not in logs, "PHI-bearing tool name leaked into the log sink"
    # Never in the observer's steps / metadata (the LangSmith-bound payload).
    assert all(phi not in (s.get("name") or "") for s in obs.steps)
    assert phi not in json.dumps(obs.metadata())
    # The span/log used the safe constant instead.
    assert any((s.get("name") or "") == "tool_unregistered_tool" for s in obs.steps)


# --- Finding 3: bounded client timeout + wall-clock deadline guard


def test_anthropic_client_constructed_with_bounded_timeout(real_llm_env, monkeypatch):
    """The graph client must carry a per-request timeout derived from settings —
    NOT the SDK's 600s default that could overrun the 20s turn deadline."""
    import anthropic

    from app.config import get_settings
    from app.graph import workers as workers_mod

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    s = get_settings()
    workers_mod._anthropic_client(s)

    assert "timeout" in captured, "client built without a timeout (SDK default is 600s)"
    assert captured["timeout"] == s.request_deadline_s
    assert captured["timeout"] < 600
    # Bounded retries too: the SDK default (2) makes one throttled call cost up
    # to 3 attempts x timeout + backoff (~67s observed live 2026-07-16) — the
    # SLOS contract is 1 transient retry.
    assert captured.get("max_retries") == 1, (
        "client built without max_retries=1 (SDK default 2 lets a throttled "
        "call stack ~3x the per-attempt timeout inside one graph span)"
    )


def test_answerer_stops_calling_after_deadline_blown(
    real_llm_env, stub_tools, monkeypatch
):
    """A model that never terminates while each call blows the wall clock must
    NOT keep hitting the LLM: after the deadline is exceeded the answerer
    force-finalizes on what it has, in a bounded number of calls."""
    from app.graph import workers as workers_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(workers_mod.time, "perf_counter", lambda: clock["t"])

    calls = {"n": 0}

    class _SlowClient:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kw):
            calls["n"] += 1
            clock["t"] += 100.0  # each call "takes" 100s → blows the 20s deadline
            return fake_resp(tool_use("get_labs", {}, id=f"t{calls['n']}"))

    monkeypatch.setattr(workers_mod, "_anthropic_client", lambda s: _SlowClient())

    # deadline defaults to perf_counter()+request_deadline_s = 1000+20 = 1020.
    resp, _ = _answerer("deadline-cid")

    # Exactly ONE model call: the first (under deadline) blows the clock, then
    # the guard finalizes without another call. Pre-fix this looped to the iter
    # cap (6 calls) forcing submit_briefing on a hung client each time.
    assert calls["n"] == 1
    assert resp.degraded is True
    assert resp.notes and "time" in resp.notes.lower()


# --- Finding 5: forced/cut-off empty finalize is degraded, not silently empty


def test_forced_empty_finalize_is_flagged_degraded(
    real_llm_env, stub_tools, fake_client_factory, monkeypatch
):
    """Deadline already blown → the answerer is FORCED to submit_briefing and the
    model returns empty claims with no notes. That must NOT be a silent empty
    success: degraded=True and a note explaining the partial result."""
    from app.config import get_settings

    monkeypatch.setenv("COPILOT_REQUEST_DEADLINE_S", "0")
    get_settings.cache_clear()
    try:
        fake_client_factory([submit_briefing_resp()])  # empty claims, notes=None
        resp, _ = _answerer("f5-cid")
        assert resp.briefing == []
        assert resp.degraded is True
        assert resp.notes and "time" in resp.notes.lower()
    finally:
        get_settings.cache_clear()


def test_legitimate_empty_refusal_not_mislabeled_degraded(
    real_llm_env, stub_tools, fake_client_factory
):
    """Guard against over-flagging: when the model submits an empty briefing on
    its OWN (auto) choice with time to spare, that is a safe refusal — not a
    cut-off — and must NOT be marked degraded."""
    fake_client_factory([submit_briefing_resp()])  # first, unforced iteration
    # Big future deadline so nothing is forced by the wall clock.
    resp, _ = _answerer("f5-safe-cid", deadline=1e18)
    assert resp.briefing == []
    assert resp.degraded is False
