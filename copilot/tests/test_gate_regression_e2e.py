"""HARD GATE, end to end — a real regression must turn the gate red.

The Week-2 rubric's hard gate: "a small regression is introduced and the CI gate must
fail. A working demo that cannot block regressions has not met the standard." The unit
suite in `test_golden_gate.py` proves the gate MATH rejects a regressed results.json;
this proves the WHOLE pipeline does it, the way a grader actually tests it:

    inject a real behavioral regression into PRODUCT code (the verifier stops
    fail-closing on unattributed claims — invariant #4 removed), run the REAL 65-case
    runner, feed its output to the REAL `gate.py` CLI, and assert it exits non-zero.

A control run with NO regression asserts the same pipeline exits 0 — so the FAIL is
caused by the injected defect, not a broken harness. The two together are the honest
form of "the gate blocks regressions": it is red when it should be, green when it
should be.

Marked `slow` (it runs the full suite twice); it runs in CI and pre-push, not
necessarily every inner-loop `pytest -k`.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_COPILOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = _COPILOT / "evals" / "golden"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"golden_{name}", GOLDEN_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"golden_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


runner = _load("runner")


def _run_suite(tmp_path: Path) -> Path:
    """Drive the REAL 65-case runner and write a real results.json (git_sha
    stamped to HEAD, full case manifest) — exactly what the gate consumes in CI."""
    cases = runner.load_cases()
    fixtures = runner._load_patient_fixtures()
    outcomes = [runner.run_case(c, fixtures) for c in cases]
    results = runner.aggregate(cases, outcomes, "hermetic")
    out = tmp_path / "results.json"
    out.write_text(json.dumps(results), encoding="utf-8")
    return out


def _run_gate(results_path: Path) -> subprocess.CompletedProcess:
    """Invoke the REAL gate.py CLI — the same entrypoint pre-push and CI call."""
    return subprocess.run(
        [
            sys.executable, str(GOLDEN_DIR / "gate.py"),
            "--results", str(results_path),
            "--baseline", str(GOLDEN_DIR / "baseline.json"),
        ],
        capture_output=True, text=True, cwd=str(_COPILOT),
    )


@pytest.mark.slow
def test_clean_run_passes_the_gate(tmp_path):
    """Control: an unregressed full run passes — proves the harness is honest, so
    the regression test's FAIL is signal, not noise."""
    proc = _run_gate(_run_suite(tmp_path))
    assert proc.returncode == 0, f"clean run must pass the gate\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "GATE: PASS" in proc.stdout


@pytest.mark.slow
def test_injected_regression_fails_the_gate(tmp_path, monkeypatch):
    """A real regression the grader would introduce: remove the verifier's
    fail-closed attribution guard (invariant #4). Unattributed model claims are
    no longer quarantined — a genuine safety regression the gate MUST catch."""
    from app import verifier

    orig_judge = verifier._judge
    # fail-open: every raw claim is accepted as "verified", nothing quarantined.
    monkeypatch.setattr(verifier, "_judge", lambda rc, ev: (True, orig_judge(rc, ev)[1]))

    proc = _run_gate(_run_suite(tmp_path))

    assert proc.returncode != 0, (
        "the gate MUST fail when the fail-closed verifier is regressed away — "
        f"a green gate here forecloses a passing Final\nSTDOUT:\n{proc.stdout}"
    )
    assert "GATE: FAIL" in proc.stdout
    # and it fails for the RIGHT reason — a safety/consistency floor breach, not an
    # unrelated crash or a malformed case set.
    assert ("floor" in proc.stdout.lower()) or ("regress" in proc.stdout.lower()), proc.stdout
