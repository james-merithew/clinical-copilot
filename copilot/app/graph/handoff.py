"""Handoff records — the graph's inspectability contract (W2_ARCHITECTURE.md §7.3).

Every transition through the graph emits one typed `Handoff` to three sinks:

1. **Structured logs** via `log_event` — **enum/ID fields ONLY** (`seq`, node
   names, `route`, `handoff_id`, `latency_ms`). The free-text `reason` /
   `payload_summary` are deliberately never passed to the log sink: the
   PHI allowlist in `correlation.py` has no key for them *and* this module
   never offers them, so free text cannot reach logs even if the allowlist
   later grows a colliding key (e.g. `reason` is allowlisted for authz events).
2. **LangSmith child runs** — via the TurnObserver hook (`record_handoff`);
   `TurnObserver.finish()` turns worker-return records into child runs of the
   turn's parent run, best-effort.
3. **`AgentResponse.handoffs`** — the full records (including free text) ride
   the response so the UI can render "how this answer was assembled".
"""

from __future__ import annotations

import logging

from ..contracts import Handoff, NodeName, RouteName
from ..correlation import get_correlation_id, log_event
from ..observability import TurnObserver

log = logging.getLogger("copilot.graph")


class HandoffLog:
    """Accumulates the turn's transition trail and fans each record out to the
    three sinks. Sequence numbers are assigned here — append-only, 0-based."""

    def __init__(self, obs: TurnObserver | None = None) -> None:
        self.handoffs: list[Handoff] = []
        self.obs = obs

    @property
    def next_seq(self) -> int:
        return len(self.handoffs)

    def record(
        self,
        from_node: NodeName,
        to_node: NodeName,
        route: RouteName,
        reason: str,
        payload_summary: str = "",
        latency_ms: float | None = None,
    ) -> Handoff:
        h = Handoff(
            seq=len(self.handoffs),
            from_node=from_node,
            to_node=to_node,
            route=route,
            reason=reason,
            payload_summary=payload_summary,
            correlation_id=get_correlation_id(),
            latency_ms=latency_ms,
        )
        self.handoffs.append(h)
        # Sink 1: structured log — enum/ID fields ONLY (never reason/payload text).
        log_event(
            log,
            "handoff",
            route=h.route,
            from_node=h.from_node,
            to_node=h.to_node,
            seq=h.seq,
            handoff_id=h.handoff_id,
            latency_ms=h.latency_ms,
        )
        # Sink 2 feed: enum/ID meta for LangSmith child runs at finish().
        if self.obs is not None:
            try:
                self.obs.record_handoff(
                    {
                        "seq": h.seq,
                        "from_node": h.from_node,
                        "to_node": h.to_node,
                        "route": h.route,
                        "handoff_id": h.handoff_id,
                        "latency_ms": h.latency_ms,
                    }
                )
            except Exception:  # noqa: BLE001 — observability must never fail a turn
                pass
        return h
