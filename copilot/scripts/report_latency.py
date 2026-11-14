#!/usr/bin/env python
"""Paced latency measurement + report harness for the Clinical Co-Pilot sidecar.

This is a MEASUREMENT tool, not a stress generator — `copilot/load/`
(k6 `chat_load.js` + `load.py`) keeps the saturation/throughput role. This
script drives each user-facing flow serially, paced under the 30 req/60s
per-IP rate limiter, and writes an honest latency report:

- (a) client wall-clock p50/p95/p99/min/max/mean per flow (`time.perf_counter`
  around each request; non-2xx responses are counted and reported but EXCLUDED
  from percentile math), and
- (b) a `/metrics/summary` scrape BEFORE and AFTER the run (server-side
  per-endpoint / per-turn / per-graph-node p50/p95), with count deltas so the
  report can confirm the scraped numbers are attributable to this run.

Honesty enforcement: `--mode {mock,live}` is REQUIRED, stamped in the report
header, and cross-checked against an observed `/chat` response body — the mock
graph path signs its output ("Mock LLM briefing (load-test mode)." in `notes`).
A claimed mode that contradicts the observed one exits nonzero, loudly.

Flows (defaults; `--n` overrides the per-flow counts, ingest stays at 3):
    GET  /ready                       x20   (no auth)
    GET  /snapshot?pid=101            x20   (patient-scope token)
    GET  /day                         x20   (day-scope token)
    POST /chat  free text             x10   ("brief me on this patient")
    POST /chat  chip care_gaps        x10
    POST /ingest (only with --ingest) x3    (demo_lab_delgado.pdf; skipped
                                             gracefully if the route 404s /
                                             ingestion is disabled)

Usage (from the repo root, with the copilot venv):
    copilot/.venv/Scripts/python.exe copilot/scripts/report_latency.py \
        --mode mock --url http://127.0.0.1:8809
    copilot/.venv/Scripts/python.exe copilot/scripts/report_latency.py \
        --mode live --url http://127.0.0.1:8808 --ingest

Outputs `latency_report.md` + `latency_report.json` into `--out-dir`
(default `copilot/load/`).

IMPORTANT: no DB or network at import time. Token minting
(`app.session_token.mint` — `get_signing_key()` reads the DB) happens inside
`main()` only; `copilot/tests/test_report_latency.py` pins this by importing
the module with sockets blocked (the `load.py` module-level-minting trap).
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

# Make `import app.*` work regardless of CWD (the package lives in copilot/).
# Path manipulation only — no app import happens until main() runs.
_COPILOT_ROOT = Path(__file__).resolve().parent.parent
if str(_COPILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_COPILOT_ROOT))

MOCK_SIGNATURE = "Mock LLM briefing (load-test mode)."
PID = 101  # demo patient the measured flows are scoped to
CHAT_MESSAGE = "brief me on this patient"
INGEST_FIXTURE = _COPILOT_ROOT / "tests" / "ingest" / "fixtures" / "demo_lab_delgado.pdf"
DEFAULT_N = {
    "ready": 20,
    "snapshot": 20,
    "day": 20,
    "chat_message": 10,
    "chat_chip_care_gaps": 10,
}
INGEST_N = 3
REQUEST_TIMEOUT_S = 60.0
# LLM-side graph nodes, for labelling the bottleneck line.
_LLM_NODES = frozenset({"supervisor", "answerer", "intake-extractor", "evidence-retriever"})


class ModeMismatch(RuntimeError):
    """--mode claim contradicts the observed /chat behavior. Fail loud: a
    latency report published under a false mode label is worse than none."""


# --- percentile math -------------------------------------------------------------


def pct(values: list[float], p: float) -> float:
    """Classic nearest-rank percentile: idx = ceil(p/100 * n) - 1 over the
    sorted values. NaN for an empty series (rendered as "-", never invented)."""
    if not values:
        return float("nan")
    vals = sorted(values)
    idx = max(0, math.ceil(p / 100.0 * len(vals)) - 1)
    return vals[min(idx, len(vals) - 1)]


# --- per-flow measurement ----------------------------------------------------------


@dataclass
class FlowStats:
    """Client wall-clock result of one serial, paced flow."""

    name: str
    requests: int = 0                       # attempted
    ok: int = 0                             # 2xx
    excluded: int = 0                       # non-2xx / transport error (counted, not timed)
    status_counts: dict[str, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)  # 2xx only
    skipped: bool = False                   # flow not run (e.g. ingest disabled)
    note: str | None = None

    def stats(self) -> dict[str, Any]:
        vals = sorted(self.latencies_ms)
        return {
            "n": self.requests,
            "ok": self.ok,
            "excluded": self.excluded,
            "p50_ms": pct(vals, 50),
            "p95_ms": pct(vals, 95),
            "p99_ms": pct(vals, 99),
            "min_ms": vals[0] if vals else float("nan"),
            "max_ms": vals[-1] if vals else float("nan"),
            "mean_ms": (sum(vals) / len(vals)) if vals else float("nan"),
            "status_counts": dict(self.status_counts),
        }


def run_flow(
    name: str,
    send: Callable[[], int | None],
    n: int,
    min_interval: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> FlowStats:
    """Run `send()` n times serially, `min_interval` apart. `send` returns the
    HTTP status (None on transport error). Non-2xx are counted + reported but
    excluded from the latency series."""
    fs = FlowStats(name=name)
    for _ in range(n):
        t0 = clock()
        try:
            status = send()
        except Exception:  # noqa: BLE001 — a refused/errored request is data, not a crash
            status = None
        dt_ms = (clock() - t0) * 1000.0
        fs.requests += 1
        key = str(status) if status is not None else "error"
        fs.status_counts[key] = fs.status_counts.get(key, 0) + 1
        if status is not None and 200 <= status < 300:
            fs.ok += 1
            fs.latencies_ms.append(dt_ms)
        else:
            fs.excluded += 1
        if min_interval > 0:
            sleep(min_interval)
    return fs


# --- /metrics/summary scrape --------------------------------------------------------


def scrape_summary(client: httpx.Client, url: str, token: str) -> dict | None:
    """GET /metrics/summary with the minted Bearer token. None on any failure —
    the report then says the scrape was unavailable instead of fabricating."""
    try:
        r = client.get(
            f"{url}/metrics/summary",
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_S,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:  # noqa: BLE001
        pass
    return None


# --- honesty: mode detection ----------------------------------------------------------


def detect_mode(chat_body: dict | None) -> str:
    """'mock' iff the /chat body carries the mock graph's signature note;
    'unknown' when there is no successful body to inspect."""
    if not isinstance(chat_body, dict):
        return "unknown"
    notes = chat_body.get("notes") or ""
    return "mock" if MOCK_SIGNATURE in notes else "live"


def check_mode(claimed: str, observed: str) -> None:
    """Raise ModeMismatch when the claim contradicts the observation.
    'unknown' (no successful /chat) is surfaced in the report, not fatal."""
    if observed in ("mock", "live") and observed != claimed:
        raise ModeMismatch(
            f"--mode {claimed} was claimed but the /chat response body indicates "
            f"'{observed}' (mock signature {'present' if observed == 'mock' else 'absent'}). "
            "Refusing to publish numbers under a false mode label."
        )


# --- bottleneck analysis ---------------------------------------------------------------


def _fmt_ms(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{float(v):.1f}"


def analyze(flows: dict[str, FlowStats], summary: dict | None) -> list[str]:
    """Computed (never hand-waved) bottleneck statements from this run's numbers.
    Every line derives from measured values already present in the report."""
    lines: list[str] = []
    nodes = (summary or {}).get("nodes") or {}
    turns = (summary or {}).get("turns") or {}
    turns_p95 = turns.get("p95_ms")

    ranked = sorted(
        ((name, s.get("p95_ms")) for name, s in nodes.items() if s.get("p95_ms") is not None),
        key=lambda kv: kv[1],
        reverse=True,
    )
    if ranked:
        lines.append(
            "Graph-node p95 ranking (server-side): "
            + ", ".join(f"{name} {_fmt_ms(p95)}ms" for name, p95 in ranked)
        )
        top_name, top_p95 = ranked[0]
        if turns_p95:
            share = 100.0 * float(top_p95) / float(turns_p95)
            tag = "the LLM dominates the turn" if top_name in _LLM_NODES \
                else "a non-LLM node dominates the turn"
            lines.append(
                f"Bottleneck: {top_name} p95 {_fmt_ms(top_p95)}ms is {share:.0f}% of "
                f"turn p95 {_fmt_ms(turns_p95)}ms -> {tag}."
            )

    chat = flows.get("chat_message")
    chat_p95 = pct(chat.latencies_ms, 95) if chat and chat.latencies_ms else None
    if chat_p95 is not None and turns_p95:
        gap = float(chat_p95) - float(turns_p95)
        lines.append(
            f"POST /chat wall-clock p95 {_fmt_ms(chat_p95)}ms vs turns.p95 "
            f"{_fmt_ms(turns_p95)}ms -> ~{gap:+.1f}ms outside the graph "
            "(middleware/serialization/HTTP; a negative gap reflects the "
            "sliding-window scope of the server counters)."
        )

    snap = flows.get("snapshot")
    snap_p95 = pct(snap.latencies_ms, 95) if snap and snap.latencies_ms else None
    if chat_p95 is not None and snap_p95 is not None:
        lines.append(
            f"/snapshot (no-LLM, app+DB only) p95 {_fmt_ms(snap_p95)}ms vs /chat p95 "
            f"{_fmt_ms(chat_p95)}ms -> ~{chat_p95 - snap_p95:.1f}ms is the LLM/graph "
            "share of a chat turn."
        )

    lines.append(
        "Saturation/throughput is NOT re-measured here — see copilot/load/RESULTS.md "
        "(k6 + load.py record: single-worker plateau ~4 req/s)."
    )
    return lines


# --- rendering ------------------------------------------------------------------------


def _flow_row(fs: FlowStats) -> str:
    s = fs.stats()
    codes = ", ".join(f"{k}:{v}" for k, v in sorted(s["status_counts"].items()))
    if fs.skipped:
        return f"| {fs.name} | - | - | - | - | - | - | - | - | - | skipped: {fs.note or ''} |"
    return (
        f"| {fs.name} | {s['n']} | {s['ok']} | {s['excluded']} | "
        f"{_fmt_ms(s['p50_ms'])} | {_fmt_ms(s['p95_ms'])} | {_fmt_ms(s['p99_ms'])} | "
        f"{_fmt_ms(s['min_ms'])} | {_fmt_ms(s['max_ms'])} | {_fmt_ms(s['mean_ms'])} | {codes} |"
    )


def _endpoint_rows(before: dict | None, after: dict | None) -> list[str]:
    rows = []
    b_eps = (before or {}).get("endpoints") or {}
    a_eps = (after or {}).get("endpoints") or {}
    for key in sorted(a_eps):
        a = a_eps[key]
        delta = a.get("count", 0) - (b_eps.get(key, {}).get("count", 0))
        rows.append(
            f"| {key} | {a.get('count', 0)} | +{delta} | {_fmt_ms(a.get('p50_ms'))} | "
            f"{_fmt_ms(a.get('p95_ms'))} | {a.get('errors_4xx', 0)} | {a.get('errors_5xx', 0)} |"
        )
    return rows


def render_markdown(
    meta: dict,
    flows: list[FlowStats],
    summary_before: dict | None,
    summary_after: dict | None,
) -> str:
    flows_by_name = {f.name: f for f in flows}
    n_desc = ", ".join(f"{k}={v}" for k, v in (meta.get("n_per_flow") or {}).items())
    out: list[str] = []
    out.append("# Latency report — Clinical Co-Pilot sidecar")
    out.append("")
    if meta.get("mode_mismatch"):
        out.append(
            "> **MODE MISMATCH — DO NOT TRUST THE MODE LABEL.** Claimed "
            f"`{meta.get('mode_claimed')}` but the /chat body indicates "
            f"`{meta.get('mode_observed')}`. This run exited nonzero."
        )
        out.append("")
    mode_note = meta.get("mode_observed", "unknown")
    verified = (
        "verified against the /chat response body"
        if mode_note in ("mock", "live") and not meta.get("mode_mismatch")
        else "UNVERIFIED (no successful /chat body observed)"
        if mode_note == "unknown"
        else "CONTRADICTED by the observed /chat body"
    )
    out.append(f"- **Mode:** `{meta.get('mode_claimed')}` ({verified})")
    out.append(f"- **Date:** {meta.get('date')}")
    out.append(f"- **URL:** {meta.get('url')}")
    out.append(f"- **Git SHA:** {meta.get('git_sha') or 'unavailable'}")
    out.append(
        f"- **Pacing:** serial, min {meta.get('min_interval_s')}s between requests "
        "(stays under the 30 req/60s per-IP rate limiter; non-2xx are counted "
        "but excluded from percentiles)"
    )
    out.append(f"- **N per flow:** {n_desc}")
    out.append(
        "- **Caveat (topology):** loopback / co-located generator — these numbers are "
        "server compute only; no WAN RTT, TLS, or ingress is included."
    )
    out.append(
        "- **Caveat (server counters):** `/metrics/summary` percentiles come from a "
        "sliding window of the most recent ~512 samples per series — they reflect "
        "recent traffic (this run, if counters deltas match), not process lifetime."
    )
    out.append("")
    out.append("## Client wall-clock latency per flow (ms)")
    out.append("")
    out.append("| flow | n | ok | excluded | p50 | p95 | p99 | min | max | mean | status codes |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for fs in flows:
        out.append(_flow_row(fs))
    out.append("")
    out.append("## Server-side `/metrics/summary` (post-run scrape)")
    out.append("")
    if summary_after is None:
        out.append("_Scrape unavailable — server-side percentiles could not be read._")
    else:
        turns = summary_after.get("turns") or {}
        out.append(
            f"- **turns:** count {turns.get('count')}, p50 {_fmt_ms(turns.get('p50_ms'))}ms, "
            f"p95 {_fmt_ms(turns.get('p95_ms'))}ms, avg {_fmt_ms(turns.get('avg_ms'))}ms, "
            f"errors {turns.get('errors')}, degraded {turns.get('degraded')}"
        )
        out.append("")
        out.append("### Graph nodes")
        out.append("")
        out.append("| node | count | p50 (ms) | p95 (ms) |")
        out.append("| --- | --- | --- | --- |")
        for name, s in sorted((summary_after.get("nodes") or {}).items()):
            out.append(
                f"| {name} | {s.get('count')} | {_fmt_ms(s.get('p50_ms'))} | "
                f"{_fmt_ms(s.get('p95_ms'))} |"
            )
        out.append("")
        out.append("### Endpoints (Δ count = attribution check: after − before scrape)")
        out.append("")
        out.append("| endpoint | count | Δ this run | p50 (ms) | p95 (ms) | 4xx | 5xx |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        out.extend(_endpoint_rows(summary_before, summary_after))
        if summary_before is None:
            out.append("")
            out.append("_Pre-run scrape unavailable — Δ counts are vs zero (unattributed)._")
    out.append("")
    out.append("## Bottleneck analysis (computed from this run)")
    out.append("")
    for line in analyze(flows_by_name, summary_after):
        out.append(f"- {line}")
    out.append("")
    return "\n".join(out)


def render_json(
    meta: dict,
    flows: list[FlowStats],
    summary_before: dict | None,
    summary_after: dict | None,
) -> dict:
    """Raw material behind the markdown: per-flow percentiles + both scrapes."""

    def _clean(v: Any) -> Any:  # NaN is not valid JSON; be explicit
        return None if isinstance(v, float) and math.isnan(v) else v

    return {
        "meta": dict(meta),
        "flows": [
            {"name": f.name, "skipped": f.skipped, "note": f.note,
             **{k: (_clean(v) if not isinstance(v, dict) else v)
                for k, v in f.stats().items()}}
            for f in flows
        ],
        "analysis": analyze({f.name: f for f in flows}, summary_after),
        "summary_before": summary_before,
        "summary_after": summary_after,
    }


# --- runtime-only helpers (DB / subprocess allowed here, never at import) ---------------


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
            cwd=str(_COPILOT_ROOT),
        ).stdout.strip() or None
    except Exception:  # noqa: BLE001 — best-effort provenance
        return None


def _mint_tokens() -> tuple[str, str]:
    """Mint one patient-scope (pid=101) and one day-scope token. Imports app.*
    lazily: get_signing_key() reads the DB (or the dev fallback), which must
    never happen at module import time."""
    from app.config import get_settings
    from app.session_token import get_signing_key, mint

    s = get_settings()
    key = get_signing_key()
    now = int(time.time())
    patient = mint(sub=s.demo_user_id, usr="latency", pid=PID, scope="patient",
                   acl="patients:demo", key=key, ttl=s.token_ttl_s, now=now)
    day = mint(sub=s.demo_user_id, usr="latency", pid=0, scope="day",
               acl="patients:demo", key=key, ttl=s.token_ttl_s, now=now)
    return patient, day


# --- main -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8808")
    ap.add_argument("--mode", choices=["mock", "live"], required=True,
                    help="what the target sidecar is running; cross-checked "
                         "against the observed /chat body — a mismatch exits nonzero")
    ap.add_argument("--n", type=int, default=None,
                    help="override the per-flow request count (ingest stays at 3)")
    ap.add_argument("--min-interval", type=float, default=2.1,
                    help="seconds between requests (default 2.1 ~= 28.6 req/min, "
                         "under the 30/min per-IP limiter)")
    ap.add_argument("--ingest", action="store_true",
                    help="also measure POST /ingest x3 with demo_lab_delgado.pdf "
                         "(skipped gracefully if the route 404s)")
    ap.add_argument("--out-dir", default=str(_COPILOT_ROOT / "load"))
    ap.add_argument("--date", default=None,
                    help="date stamp for the report header (default: system clock)")
    args = ap.parse_args(argv)

    counts = {k: (args.n if args.n else v) for k, v in DEFAULT_N.items()}
    patient_token, day_token = _mint_tokens()

    flows: list[FlowStats] = []
    chat_body_holder: dict[str, Any] = {"body": None}

    with httpx.Client(http2=False, timeout=REQUEST_TIMEOUT_S) as client:
        summary_before = scrape_summary(client, args.url, patient_token)

        def _get(path: str, token: str | None) -> Callable[[], int | None]:
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            def send() -> int | None:
                return client.get(f"{args.url}{path}", headers=headers).status_code

            return send

        def _chat(body: dict) -> Callable[[], int | None]:
            def send() -> int | None:
                r = client.post(
                    f"{args.url}/chat", json=body,
                    headers={"Authorization": f"Bearer {patient_token}"},
                )
                if r.status_code == 200 and chat_body_holder["body"] is None:
                    try:
                        chat_body_holder["body"] = r.json()
                    except Exception:  # noqa: BLE001
                        pass
                return r.status_code

            return send

        print(f"[report_latency] mode={args.mode} url={args.url} "
              f"pacing={args.min_interval}s counts={counts} ingest={args.ingest}")

        specs: list[tuple[str, Callable[[], int | None]]] = [
            ("ready", _get("/ready", None)),
            ("snapshot", _get(f"/snapshot?pid={PID}", patient_token)),
            ("day", _get("/day", day_token)),
            ("chat_message", _chat({"pid": PID, "message": CHAT_MESSAGE})),
            ("chat_chip_care_gaps", _chat({"pid": PID, "chip_intent": "care_gaps"})),
        ]
        for name, send in specs:
            print(f"[report_latency] flow {name} x{counts[name]} ...")
            flows.append(run_flow(name, send, counts[name], args.min_interval))

        if args.ingest:
            flows.append(_run_ingest(client, args.url, patient_token, args.min_interval))

        summary_after = scrape_summary(client, args.url, patient_token)

    observed = detect_mode(chat_body_holder["body"])
    mismatch = False
    try:
        check_mode(claimed=args.mode, observed=observed)
    except ModeMismatch as exc:
        mismatch = True
        print(f"\n[report_latency] *** MODE MISMATCH *** {exc}\n", file=sys.stderr)

    meta = {
        "mode_claimed": args.mode,
        "mode_observed": observed,
        "mode_mismatch": mismatch,
        "date": args.date or time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": args.url,
        "git_sha": _git_sha(),
        "min_interval_s": args.min_interval,
        "n_per_flow": {**counts, **({"ingest": INGEST_N} if args.ingest else {})},
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "latency_report.md"
    json_path = out_dir / "latency_report.json"
    md_path.write_text(render_markdown(meta, flows, summary_before, summary_after),
                       encoding="utf-8", newline="\n")
    json_path.write_text(
        json.dumps(render_json(meta, flows, summary_before, summary_after), indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"[report_latency] wrote {md_path} and {json_path}")

    if mismatch:
        print("[report_latency] exiting nonzero: the report is stamped MODE MISMATCH "
              "and must not be published as-is.", file=sys.stderr)
        return 2
    if observed == "unknown":
        print("[report_latency] WARNING: no successful /chat body observed — the mode "
              "label is UNVERIFIED (stamped in the report).", file=sys.stderr)
    return 0


def _run_ingest(client: httpx.Client, url: str, token: str, min_interval: float) -> FlowStats:
    """POST /ingest x3 with the committed demo lab PDF. If the route is disabled
    (404) or the fixture is missing, return a skipped flow instead of failing."""
    if not INGEST_FIXTURE.exists():
        return FlowStats(name="ingest", skipped=True, note="fixture missing")
    data = INGEST_FIXTURE.read_bytes()

    def send() -> int | None:
        r = client.post(
            f"{url}/ingest",
            files={"file": (INGEST_FIXTURE.name, data, "application/pdf")},
            data={"pid": str(PID), "doc_type": "lab_report"},
            headers={"Authorization": f"Bearer {token}"},
        )
        return r.status_code

    # Probe once: a 404 means ingestion is disabled (the route "does not exist").
    probe = run_flow("ingest", send, 1, min_interval)
    if probe.status_counts.get("404"):
        return FlowStats(name="ingest", skipped=True,
                         note="POST /ingest returned 404 (ingestion disabled)")
    rest = run_flow("ingest", send, INGEST_N - 1, min_interval)
    probe.requests += rest.requests
    probe.ok += rest.ok
    probe.excluded += rest.excluded
    probe.latencies_ms.extend(rest.latencies_ms)
    for k, v in rest.status_counts.items():
        probe.status_counts[k] = probe.status_counts.get(k, 0) + v
    return probe


if __name__ == "__main__":
    raise SystemExit(main())
