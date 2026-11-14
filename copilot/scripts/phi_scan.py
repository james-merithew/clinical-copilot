#!/usr/bin/env python
"""Standalone PHI-detection scanner — the named CI check for invariant #8.

Fails closed (exit 1) if any Protected Health Information reaches a surface that MUST be
PHI-free: the runtime log/trace stream and the committed eval/observability OUTPUT
artifacts (results, panel/gate reports, metrics dumps).

It deliberately does NOT scan INPUT fixtures (patients.json, ground_truth.json, the
case YAMLs, the fixture PDFs) or human documentation — those legitimately carry
synthetic PHI-shaped values as test material. The invariant this guards is narrower and
sharper: none of that PHI may survive into an observability output. On a scrubbed
machine surface, a surname or a drug name IS a leak, because the scrubber is supposed to
have reduced everything to correlation ids, source keys, counts, and enums.

Two detector layers:
  - sentinels: the exact synthetic-PHI tokens the demo fixtures use (surnames, drug and
    condition names, loaded from the golden fixtures) — high precision; any occurrence
    in a machine output is a leak;
  - structural: SSN / email / US-phone / ISO-DOB shapes — defense in depth for PHI the
    fixtures do not enumerate.

Findings report file:line + which detector fired, NEVER the matched value — the scanner
must not itself become the PHI sink it is hunting for.

Usage:
    python copilot/scripts/phi_scan.py            # runtime capture + committed artifacts
    python copilot/scripts/phi_scan.py --no-runtime   # committed artifacts only (fast)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

COPILOT_DIR = Path(__file__).resolve().parents[1]
if str(COPILOT_DIR) not in sys.path:
    sys.path.insert(0, str(COPILOT_DIR))

GOLDEN_DIR = COPILOT_DIR / "evals" / "golden"
FIXTURES_DIR = GOLDEN_DIR / "fixtures"

# Observability / eval-OUTPUT artifacts that must never contain PHI. Globs, relative to
# the copilot/ dir. Missing files are simply skipped (not every run has produced them).
COMMITTED_TARGET_GLOBS = [
    "evals/golden/results*.json",
    "evals/golden/panel_report.json",
    "evals/golden/panel_report.md",
    "evals/golden/gate-report.md",
    "evals/adversarial/results*.json",
    "logs/**/*.log",
    "logs/**/*.jsonl",
]

# Structural PHI shapes (applied to machine outputs only). Kept deliberately high-signal
# so a scrubbed artifact — ids/counts/enums — never trips them.
STRUCTURAL = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "us_phone": re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}"),
    "iso_dob": re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b"),
}


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    detector: str  # e.g. "sentinel" or "structural:ssn" — never the matched value


def load_sentinels() -> list[str]:
    """The exact synthetic-PHI tokens the demo fixtures use (the same set the golden
    `no_phi_in_logs` rubric checks). Loaded, not hard-coded, so the two stay in step."""
    pj = json.loads((FIXTURES_DIR / "patients.json").read_text(encoding="utf-8"))
    return [str(s) for s in pj.get("phi_sentinels", []) if str(s).strip()]


def scan_text(text: str, source: str, sentinels: list[str]) -> list[Finding]:
    """Match sentinels CASE-SENSITIVELY (as the golden `no_phi_in_logs` rubric does):
    the sentinels are the capitalized PHI value-forms ("Hemoglobin", "Fernsby"), and a
    real leak preserves that casing. The lowercase source-key SLUGS that legitimately
    ride in logs (e.g. the field_id `hemoglobin_a1c.value` — an allowlisted key, not a
    value) must not trip the scan. Structural shapes are matched by their own regexes."""
    findings: list[Finding] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for s in sentinels:
            if s in line:
                findings.append(Finding(source, i, "sentinel"))
                break  # one hit per line is enough — don't echo how many
        for name, rx in STRUCTURAL.items():
            if rx.search(line):
                findings.append(Finding(source, i, f"structural:{name}"))
    return findings


def capture_runtime_logs() -> str:
    """Run a broad hermetic slice — ingest, evidence retrieval, a chat turn, and a
    degraded (tool-failure) turn — capturing the REAL JSON log stream exactly as the
    production sink formats it, then hand it back for scanning. This is the high-value
    scan: it proves the scrubber holds on live output, not just on committed files."""
    import io
    import logging
    import os

    from app.correlation import _JsonFormatter

    runner = _load_runner()

    env = dict(runner._HERMETIC_ENV)
    env.update(runner._INGEST_ENV)
    saved = runner._apply_env(env)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    prev_level = root.level
    root.setLevel(logging.INFO)
    try:
        cases = runner.load_cases()
        fixtures = runner._load_patient_fixtures()
        by_id = {c.id: c for c in cases}
        # one case per execution flavor that emits logs across the tiers
        for cid in ("ex-002", "ev-002", "ct-002", "md-004"):
            if cid in by_id:
                runner.run_case(by_id[cid], fixtures)
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)
        runner._restore_env(saved)
        os.environ.pop("PYTHONWARNINGS", None)
    return buf.getvalue()


def _load_runner():
    spec = importlib.util.spec_from_file_location("golden_runner_phi", GOLDEN_DIR / "runner.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["golden_runner_phi"] = mod
    spec.loader.exec_module(mod)
    return mod


def scan(runtime: bool = True) -> list[Finding]:
    sentinels = load_sentinels()
    findings: list[Finding] = []

    for glob in COMMITTED_TARGET_GLOBS:
        for path in sorted(COPILOT_DIR.glob(glob)):
            if not path.is_file():
                continue
            rel = path.relative_to(COPILOT_DIR).as_posix()
            findings += scan_text(path.read_text(encoding="utf-8", errors="replace"), rel, sentinels)

    if runtime:
        findings += scan_text(capture_runtime_logs(), "<runtime-log-capture>", sentinels)

    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fail-closed PHI scanner over observability outputs")
    ap.add_argument("--no-runtime", action="store_true", help="skip the live log capture (committed artifacts only)")
    args = ap.parse_args(argv)

    findings = scan(runtime=not args.no_runtime)
    if findings:
        print(f"PHI-SCAN: FAIL — {len(findings)} finding(s) on PHI-free surfaces:")
        for f in findings:
            print(f"  {f.source}:{f.line}  [{f.detector}]")
        print("A machine/observability surface leaked PHI. Scrub it to ids/counts/enums.")
        return 1
    print("PHI-SCAN: PASS — no PHI on runtime logs or committed observability artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
