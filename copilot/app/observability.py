"""Observability — LangSmith tracing + PHI-safe metrics.

Design rules:
- **Best-effort, never fatal.** If LangSmith is misconfigured or down, tracing
  degrades to structured logs; it must NEVER raise into a `/chat` turn.
- **PHI discipline.** We attach IDs, PKs, counts, timings, tokens, and cost to
  traces — the *inputs/outputs* the agent sees are clinical content and are only
  ever synthetic demo data (documented control; production needs a BAA with the
  tracer or redaction before tracing).
- The structured logs alone can reconstruct a full trace (brief Eng-Req); the
  LangSmith run is the real-time dashboard on top.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import get_settings
from .correlation import get_correlation_id, log_event

log = logging.getLogger("copilot.obs")


def configure_observability() -> None:
    """Wire LangSmith via env so the SDK picks it up. Safe if keys are absent."""
    s = get_settings()
    if s.langsmith_ready:
        os.environ.setdefault("LANGSMITH_API_KEY", s.langsmith_api_key)
        os.environ.setdefault("LANGSMITH_ENDPOINT", s.langsmith_endpoint)
        os.environ.setdefault("LANGSMITH_PROJECT", s.langsmith_project)
        os.environ.setdefault("LANGSMITH_TRACING", "true")


def ls_ready() -> bool:
    """Cheap reachability probe for /ready. Never raises."""
    s = get_settings()
    if not s.langsmith_ready:
        return False
    try:
        from langsmith import Client

        # Lightweight auth check — does NOT run a completion.
        Client(api_key=s.langsmith_api_key, api_url=s.langsmith_endpoint).info
        return True
    except Exception as exc:  # noqa: BLE001
        log_event(log, "langsmith_unreachable", error=type(exc).__name__)
        return False


class TurnObserver:
    """Accumulates PHI-free metrics for one /chat turn and ships them to
    LangSmith as a single run (with an ordered `steps` timeline) + a structured
    summary log line. All emission is best-effort."""

    def __init__(self, pid: int, kind: str) -> None:
        self.pid = pid
        self.kind = kind  # "message" | "chip"
        self.t0 = time.perf_counter()
        self.steps: list[dict[str, Any]] = []
        self.tool_calls = 0
        self.tool_failures = 0
        self.retries = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.verified = 0
        self.quarantined = 0
        self.constraint_flags = 0
        # Week 2 extension: deterministic critic counters (counts only —
        # surfaced via metadata()/LangSmith, not on the log allowlist).
        self.critic_rejected = 0
        self.critic_stripped = 0
        self.degraded = False
        # Guideline-retrieval degradation (Cohere down → BM25-only / Noop). PHI-
        # free: provider/reranker names + a boolean. Surfaced so a mid-turn RAG
        # outage is a reported state on the trace/metadata, never silent (W2 §9.7).
        self.retrieval_degraded = False
        self.retrieval_provider: str | None = None
        self.rerank_provider: str | None = None
        self.error: str | None = None
        # Week 2 (graph): per-node aggregates + the enum-only handoff trail.
        self.node_ms: dict[str, float] = {}
        self.node_tokens: dict[str, dict[str, int]] = {}
        self.node_cost: dict[str, float] = {}
        self.handoffs_meta: list[dict[str, Any]] = []

    @contextmanager
    def span(self, name: str, node: str | None = None) -> Iterator[None]:
        """Timed step with optional graph-node attribution. Steps become
        {name, node, ms}; node-attributed spans roll up into `node_ms`."""
        s0 = time.perf_counter()
        try:
            yield
        finally:
            ms = round((time.perf_counter() - s0) * 1000, 1)
            self.steps.append({"name": name, "node": node, "ms": ms})
            if node:
                self.node_ms[node] = round(self.node_ms.get(node, 0.0) + ms, 1)

    def step(self, name: str) -> Iterator[None]:
        """Week 1 alias — a span with no node attribution."""
        return self.span(name)

    def record_tokens(self, usage: Any, node: str | None = None) -> None:
        try:
            tin = int(getattr(usage, "input_tokens", 0) or 0)
            tout = int(getattr(usage, "output_tokens", 0) or 0)
            self.input_tokens += tin
            self.output_tokens += tout
            if node:
                agg = self.node_tokens.setdefault(node, {"input": 0, "output": 0})
                agg["input"] += tin
                agg["output"] += tout
        except Exception:  # noqa: BLE001
            pass

    def record_cost(self, usd: float, node: str | None = None) -> None:
        try:
            self.cost_usd += float(usd)
            if node:
                self.node_cost[node] = round(self.node_cost.get(node, 0.0) + float(usd), 6)
        except Exception:  # noqa: BLE001
            pass

    def record_handoff(self, meta: dict[str, Any]) -> None:
        """Enum/ID-only handoff metadata from HandoffLog (sink 2 feed) — used at
        finish() to emit LangSmith child runs. Never contains free text."""
        self.handoffs_meta.append(meta)

    @property
    def latency_ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 1)

    def metadata(self) -> dict[str, Any]:
        return {
            "correlation_id": get_correlation_id(),
            "pid": self.pid,
            "kind": self.kind,
            "latency_ms": self.latency_ms,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "verified_claims": self.verified,
            "quarantined_claims": self.quarantined,
            "constraint_flags": self.constraint_flags,
            "critic_rejected": self.critic_rejected,
            "critic_stripped": self.critic_stripped,
            "verification_passed": self.quarantined == 0,
            "degraded": self.degraded,
            "retrieval_degraded": self.retrieval_degraded,
            "provider": self.retrieval_provider,
            "rerank_provider": self.rerank_provider,
            "error": self.error,
            "steps": self.steps,
            # Week 2 (graph): per-node aggregates + enum-only handoff trail.
            # Not on the log allowlist (they reach LangSmith metadata only).
            "node_ms": self.node_ms,
            "node_tokens": self.node_tokens,
            "node_cost": self.node_cost,
            "handoffs": self.handoffs_meta,
        }

    def finish(self) -> None:
        meta = self.metadata()
        # Always: structured log (trace reconstructable from logs alone).
        log_event(log, "chat_turn", **meta)
        # Week 2 (D6b dashboard): feed the PHI-free in-process counters —
        # per-node latency + routing outcomes come straight from this turn's
        # aggregates. `record_turn` is never-fatal by construction.
        from . import metrics_summary

        metrics_summary.record_turn(
            kind=self.kind,
            latency_ms=self.latency_ms,
            node_ms=self.node_ms,
            routes=[h.get("route") for h in self.handoffs_meta],
            error=self.error,
            verified=self.verified,
            quarantined=self.quarantined,
            degraded=self.degraded,
        )
        # Best-effort: ship a LangSmith run for the dashboard.
        s = get_settings()
        if not s.langsmith_ready:
            return
        try:
            import uuid

            from langsmith import Client

            client = Client(api_key=s.langsmith_api_key, api_url=s.langsmith_endpoint)
            parent_run_id = uuid.uuid4()
            client.create_run(
                id=parent_run_id,
                name="chat_turn",
                run_type="chain",
                project_name=s.langsmith_project,
                inputs={"pid": self.pid, "kind": self.kind},
                outputs={
                    "verified_claims": self.verified,
                    "quarantined_claims": self.quarantined,
                    "constraint_flags": self.constraint_flags,
                },
                extra={"metadata": meta},
                error=self.error,
            )
            # Week 2 (graph): each worker invocation = a child run of the turn,
            # reconstructable by correlation ID. Best-effort, enum/ID data only.
            for h in self.handoffs_meta:
                if h.get("route") != "worker_return":
                    continue
                worker = h.get("from_node")
                try:
                    client.create_run(
                        id=uuid.uuid4(),
                        parent_run_id=parent_run_id,
                        name=f"worker:{worker}",
                        run_type="chain",
                        project_name=s.langsmith_project,
                        inputs={"seq": h.get("seq"), "correlation_id": meta["correlation_id"]},
                        outputs={
                            "latency_ms": h.get("latency_ms"),
                            "node_ms": self.node_ms.get(worker or ""),
                            "node_tokens": self.node_tokens.get(worker or ""),
                        },
                        extra={"metadata": {"handoff_id": h.get("handoff_id")}},
                    )
                except Exception:  # noqa: BLE001 — one bad child never kills the rest
                    continue
        except Exception as exc:  # noqa: BLE001
            log_event(log, "langsmith_emit_failed", error=type(exc).__name__)
