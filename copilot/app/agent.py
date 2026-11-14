"""Agent entrypoint — Week 2: a thin delegate into the supervisor+workers graph.

The Week 1 single-agent tool loop was EXTRACTED MECHANICALLY into
`copilot/app/graph/workers.py` as the graph's `answerer` node (same code,
new home); routing, budgets, and handoff records live in
`copilot/app/graph/supervisor.py`. `run_turn` keeps its exact signature, so
`/chat`, `/snapshot`, `/day`, and mock mode are unchanged callers — a plain
mock-mode question still produces the byte-compatible Week 1 briefing.

Preserved invariants (now enforced one level down, unchanged):
- pid injected server-side, never a tool arg (injection guard);
- forced terminal `submit_briefing` at iteration cap / wall-clock deadline;
- degraded template briefing on total failure — the loop NEVER raises;
- `_finalize` (verifier + degraded semantics) is the single exit;
- `COPILOT_MOCK_LLM=true` stays deterministic and token-free.
"""

from __future__ import annotations

from .contracts import AgentResponse
from .graph.supervisor import run_graph_turn

# Re-exported for compatibility: these Week 1 symbols moved MECHANICALLY into
# the graph package (the answerer node) — same code, new home.
from .graph.workers import (  # noqa: F401
    SUBMIT_TOOL,
    _finalize,
    _mock_briefing,
    _register,
    _run_tool,
    _Turn,
)
from .observability import TurnObserver
from .tools import ToolContext


def run_turn(
    ctx: ToolContext, user_text: str | None, history: list[dict], obs: TurnObserver
) -> AgentResponse:
    return run_graph_turn(ctx, user_text, history, obs)
