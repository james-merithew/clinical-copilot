"""In-process, PHI-free metrics accumulator — the data behind `GET
/metrics/summary` and the `/dashboard` page (W2_ARCHITECTURE.md §9.3, D6b).

Counters and rates ONLY. Keys are route templates (`POST /chat`), graph-node
enums (`supervisor`), and route enums (`finalize`); values are counts and
millisecond aggregates. No names, filenames, quotes, or free text ever enter
this module — a grader can read the summary without a PHI review.

Design rules:
- **Never fatal.** Every module-level `record_*` swallows its own exceptions:
  a metrics bug must not break a `/chat` turn or an ingest.
- **Bounded memory.** Latency series keep a sliding window of the most recent
  `_MAX_SAMPLES` observations (p50/p95 reflect recent behavior); totals and
  counts are exact for the process lifetime.
- **Process-local by design.** The sidecar is a single service; in-process
  counters answer "is the system healthy" without an external metrics stack.
  They reset on restart — `generated_ts`/`uptime_s` make that explicit.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

# Sliding-window size per latency series (recent behavior, bounded memory).
_MAX_SAMPLES = 512

# Last golden-eval run, written by copilot/evals/golden/runner.py. Only the
# numeric/enum aggregate fields pass through (never per-case content).
EVAL_RESULTS_PATH = Path(__file__).resolve().parent.parent / "evals" / "golden" / "results.json"


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile over an already-sorted window."""
    if not sorted_vals:
        return None
    idx = int(round(p / 100.0 * (len(sorted_vals) - 1)))
    idx = min(len(sorted_vals) - 1, max(0, idx))
    return round(float(sorted_vals[idx]), 1)


class _LatencySeries:
    """Exact count/total + a bounded sample window for percentiles."""

    __slots__ = ("window", "count", "total_ms")

    def __init__(self) -> None:
        self.window: deque[float] = deque(maxlen=_MAX_SAMPLES)
        self.count = 0
        self.total_ms = 0.0

    def add(self, ms: float) -> None:
        ms = float(ms)
        self.window.append(ms)
        self.count += 1
        self.total_ms += ms

    def stats(self) -> dict[str, Any]:
        vals = sorted(self.window)
        return {
            "count": self.count,
            "avg_ms": round(self.total_ms / self.count, 1) if self.count else None,
            "p50_ms": _percentile(vals, 50),
            "p95_ms": _percentile(vals, 95),
        }


class MetricsSummary:
    """Thread-safe accumulator. One instance per process (see module tail)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init_state()

    # -- state ---------------------------------------------------------------

    def _init_state(self) -> None:
        self._started = time.time()
        # "METHOD /route/template" -> {count, errors_4xx, errors_5xx, lat}
        self._requests: dict[str, dict[str, Any]] = {}
        self._turns: dict[str, int] = {
            "count": 0, "errors": 0, "degraded": 0,
            "verified_claims": 0, "quarantined_claims": 0,
        }
        self._turn_kinds: dict[str, int] = {}
        self._turn_lat = _LatencySeries()
        self._nodes: dict[str, _LatencySeries] = {}
        self._routes: dict[str, int] = {}
        self._ingest: dict[str, int] = {
            "count": 0, "persisted": 0, "deduplicated": 0, "needs_review": 0,
            "failed": 0, "fields_total": 0, "fields_extracted": 0,
            "fields_failed_verification": 0,
        }
        self._retrieval: dict[str, int] = {
            "queries": 0, "hits": 0, "degraded": 0, "rewritten": 0,
        }

    def reset(self) -> None:
        with self._lock:
            self._init_state()

    # -- recorders (all keys are templates/enums; all values numeric) ---------

    def record_request(self, *, path: str, method: str, status: int, ms: float) -> None:
        key = f"{method.upper()} {path}"
        with self._lock:
            entry = self._requests.get(key)
            if entry is None:
                entry = {"count": 0, "errors_4xx": 0, "errors_5xx": 0, "lat": _LatencySeries()}
                self._requests[key] = entry
            entry["count"] += 1
            if 400 <= int(status) < 500:
                entry["errors_4xx"] += 1
            elif int(status) >= 500:
                entry["errors_5xx"] += 1
            entry["lat"].add(ms)

    def record_turn(
        self,
        *,
        kind: str,
        latency_ms: float,
        node_ms: dict[str, float] | None = None,
        routes: list[str] | None = None,
        error: str | None = None,
        verified: int = 0,
        quarantined: int = 0,
        degraded: bool = False,
    ) -> None:
        with self._lock:
            self._turns["count"] += 1
            if error:
                self._turns["errors"] += 1
            if degraded:
                self._turns["degraded"] += 1
            self._turns["verified_claims"] += int(verified)
            self._turns["quarantined_claims"] += int(quarantined)
            self._turn_kinds[kind] = self._turn_kinds.get(kind, 0) + 1
            self._turn_lat.add(latency_ms)
            for node, ms in (node_ms or {}).items():
                series = self._nodes.get(node)
                if series is None:
                    series = self._nodes[node] = _LatencySeries()
                series.add(ms)
            for route in routes or []:
                if route:
                    self._routes[route] = self._routes.get(route, 0) + 1

    def record_ingest(
        self,
        *,
        status: str,
        deduplicated: bool,
        fields_total: int,
        fields_extracted: int,
        fields_failed_verification: int,
    ) -> None:
        with self._lock:
            self._ingest["count"] += 1
            if status in ("persisted", "deduplicated", "needs_review", "failed"):
                self._ingest[status] += 1
            if deduplicated and status != "deduplicated":
                self._ingest["deduplicated"] += 1
            self._ingest["fields_total"] += int(fields_total)
            self._ingest["fields_extracted"] += int(fields_extracted)
            self._ingest["fields_failed_verification"] += int(fields_failed_verification)

    def record_retrieval(self, *, hit: bool, degraded: bool = False, rewritten: bool = False) -> None:
        with self._lock:
            self._retrieval["queries"] += 1
            if hit:
                self._retrieval["hits"] += 1
            if degraded:
                self._retrieval["degraded"] += 1
            if rewritten:
                # T6: query-expansion adoption (flag only — never query text).
                self._retrieval["rewritten"] += 1

    # -- summary ---------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        with self._lock:
            endpoints = {
                key: {
                    "count": e["count"],
                    "errors_4xx": e["errors_4xx"],
                    "errors_5xx": e["errors_5xx"],
                    **{k: v for k, v in e["lat"].stats().items() if k != "count"},
                }
                for key, e in sorted(self._requests.items())
            }
            turns = {**self._turns, "by_kind": dict(self._turn_kinds),
                     **{k: v for k, v in self._turn_lat.stats().items() if k != "count"}}
            nodes = {name: s.stats() for name, s in sorted(self._nodes.items())}
            routing = dict(sorted(self._routes.items()))
            ing = dict(self._ingest)
            ing["field_pass_rate"] = (
                round(ing["fields_extracted"] / ing["fields_total"], 4)
                if ing["fields_total"] else None
            )
            ret = dict(self._retrieval)
            ret["hit_rate"] = (
                round(ret["hits"] / ret["queries"], 4) if ret["queries"] else None
            )
            uptime = round(time.time() - self._started, 1)
        return {
            "generated_ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "uptime_s": uptime,
            "endpoints": endpoints,
            "turns": turns,
            "nodes": nodes,
            "routing": routing,
            "ingestion": ing,
            "retrieval": ret,
            "evals": _read_eval_results(),
        }


def _read_eval_results() -> dict[str, Any] | None:
    """Numeric/enum aggregates from the last golden run — never per-case data
    (case ids/notes are free text and stay out by construction)."""
    try:
        raw = json.loads(Path(EVAL_RESULTS_PATH).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent/corrupt file = no eval section
        return None
    out: dict[str, Any] = {}
    for key in ("ts", "git_sha", "mode"):
        v = raw.get(key)
        if isinstance(v, str):
            out[key] = v[:64]
    per_cat = raw.get("per_category")
    if isinstance(per_cat, dict):
        out["per_category"] = {
            str(cat)[:64]: {
                k: v for k, v in stats.items()
                if k in ("n", "passed", "rate", "skipped") and isinstance(v, (int, float, type(None)))
            }
            for cat, stats in per_cat.items()
            if isinstance(stats, dict)
        }
    overall = raw.get("overall")
    if isinstance(overall, dict):
        out["overall"] = {
            k: v for k, v in overall.items()
            if k in ("total", "active", "passed", "failed", "errors", "skipped", "rate")
            and isinstance(v, (int, float, type(None)))
        }
    return out or None


# ---- module-level singleton + never-fatal facade --------------------------------

_METRICS = MetricsSummary()


def record_request(**kw: Any) -> None:
    try:
        _METRICS.record_request(**kw)
    except Exception:  # noqa: BLE001 — metrics must never break a request
        pass


def record_turn(**kw: Any) -> None:
    try:
        _METRICS.record_turn(**kw)
    except Exception:  # noqa: BLE001
        pass


def record_ingest(**kw: Any) -> None:
    try:
        _METRICS.record_ingest(**kw)
    except Exception:  # noqa: BLE001
        pass


def record_retrieval(**kw: Any) -> None:
    try:
        _METRICS.record_retrieval(**kw)
    except Exception:  # noqa: BLE001
        pass


def summary() -> dict[str, Any]:
    return _METRICS.summary()


def reset() -> None:
    _METRICS.reset()
