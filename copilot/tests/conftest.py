"""Shared hermetic fixtures for the graph test suite (no DB, no LLM, no keys).

Plain helpers (stub tools, FakeAnthropic, block builders) live in
`graph_utils.py`; this file only wires them into fixtures:
- `stub_tools` — patches the 4 patient tools into TOOL_FUNCS in place;
- `mock_llm_env` / `real_llm_env` — flip mock mode, reset the settings cache;
- `fake_client_factory` — installs a scripted FakeAnthropic behind the
  `_anthropic_client` seam in app.graph.workers;
- `log_capture` — captures the JSON log stream as the real sink sees it.
"""

from __future__ import annotations

import pytest

from graph_utils import (
    FakeAnthropic,
    stub_care_transitions,
    stub_labs,
    stub_medications,
    stub_problems_allergies,
)


def pytest_configure(config):
    """Register custom markers so `-m` selection works and no UnknownMark warning
    fires. `slow`: full-suite end-to-end tests (e.g. the hard-gate regression proof)
    that run the 65-case runner; deselect with `-m "not slow"` for a fast inner loop."""
    config.addinivalue_line(
        "markers", "slow: end-to-end test that runs the full golden suite (seconds)"
    )


@pytest.fixture
def stub_tools(monkeypatch):
    """Patch the 4 Week 1 patient tools in the shared TOOL_FUNCS registry."""
    from app.tools import TOOL_FUNCS

    monkeypatch.setitem(TOOL_FUNCS, "get_care_transitions", stub_care_transitions)
    monkeypatch.setitem(TOOL_FUNCS, "get_medications", stub_medications)
    monkeypatch.setitem(TOOL_FUNCS, "get_problems_allergies", stub_problems_allergies)
    monkeypatch.setitem(TOOL_FUNCS, "get_labs", stub_labs)
    return TOOL_FUNCS


@pytest.fixture
def mock_llm_env(monkeypatch):
    """COPILOT_MOCK_LLM=true for the test, settings cache reset around it."""
    from app.config import get_settings

    monkeypatch.setenv("COPILOT_MOCK_LLM", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().mock_llm is True
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def real_llm_env(monkeypatch):
    """Mock mode OFF (graph takes the LLM path) with a dummy key; the actual
    client is always replaced via the `_anthropic_client` seam."""
    from app.config import get_settings

    monkeypatch.setenv("COPILOT_MOCK_LLM", "false")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "test-key-never-used")
    get_settings.cache_clear()
    try:
        assert get_settings().mock_llm is False
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def fake_client_factory(monkeypatch):
    """Install a scripted FakeAnthropic behind app.graph.workers._anthropic_client
    (the single construction seam used by supervisor, workers, and answerer)."""
    from app.graph import workers as workers_mod

    def install(responses) -> FakeAnthropic:
        fake = FakeAnthropic(responses)
        monkeypatch.setattr(workers_mod, "_anthropic_client", lambda s: fake)
        return fake

    return install


@pytest.fixture
def log_capture():
    """Capture the JSON log stream exactly as the real sink would see it."""
    import io
    import logging

    from app.correlation import _JsonFormatter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    prev_level = root.level
    root.setLevel(logging.INFO)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)
