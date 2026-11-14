"""In-house supervisor + workers graph (Week 2, design decision D5).

Three small modules whose entire control flow is readable in one sitting
(W2_ARCHITECTURE.md §7):

- `handoff.py`  — HandoffLog: every graph transition becomes a typed Handoff
  emitted to three sinks (enum-only structured log, LangSmith child run via the
  TurnObserver hook, `AgentResponse.handoffs`).
- `workers.py`  — the Week 1 tool loop extracted mechanically as the `answerer`
  node, plus two bounded specialist workers with restricted tool subsets.
- `supervisor.py` — `run_graph_turn`: one routing LLM with exactly 3 routing
  tools, route/deadline budgets, forced finalization, deterministic mock path.
"""

from .supervisor import run_graph_turn

__all__ = ["run_graph_turn"]
