"""Tests for the standalone PHI-detection scanner (copilot/scripts/phi_scan.py).

The scanner is the named CI check for invariant #8 ("no PHI in observability"). These
tests prove three things a green scanner alone could not:
  - it has TEETH — planted PHI (sentinel value-form, SSN, email, phone, DOB) is caught;
  - it is VALUE-BLIND — a finding never carries the raw PHI it found (else the detector
    becomes the leak);
  - it does not false-positive on the allowlisted source-key SLUGS that legitimately
    ride in logs (the capitalized value-form is PHI; the lowercase slug is a key).

The live "runtime logs are scrubbed clean" assertion runs the real capture and is marked
`slow`; the scanner itself also runs in pre-push and CI as a script step.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_COPILOT = Path(__file__).resolve().parents[1]


def _load_scanner():
    path = _COPILOT / "scripts" / "phi_scan.py"
    spec = importlib.util.spec_from_file_location("phi_scan_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["phi_scan_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


phi = _load_scanner()
SENTINELS = phi.load_sentinels()


def test_detector_catches_planted_phi():
    line = '{"event":"turn","leak":"patient Fernsby SSN 123-45-6789 mail a.b@c.com ph (217) 555-0181 dob 1961-04-12"}'
    fired = {f.detector for f in phi.scan_text(line, "<planted>", SENTINELS)}
    assert "sentinel" in fired  # the surname value-form
    assert "structural:ssn" in fired
    assert "structural:email" in fired
    assert "structural:us_phone" in fired
    assert "structural:iso_dob" in fired


def test_findings_are_value_blind():
    line = "patient Fernsby ssn 123-45-6789"
    findings = phi.scan_text(line, "<planted>", SENTINELS)
    assert findings
    for f in findings:
        blob = f"{f.source} {f.line} {f.detector}"
        assert "Fernsby" not in blob and "123-45-6789" not in blob


def test_lowercase_sourcekey_slug_is_not_flagged():
    """The allowlisted field_id slug that DOES ride in logs must not trip the scan:
    'Hemoglobin' (PHI value-form) is a sentinel, but 'hemoglobin_a1c.value' (a key) is
    not — the case-sensitive match is what separates them."""
    line = '{"event":"extract_field_outcome","field_id":"hemoglobin_a1c.value","status":"extracted"}'
    assert phi.scan_text(line, "<key>", SENTINELS) == []


def test_committed_artifacts_scan_is_clean():
    """No committed observability/eval-output artifact carries PHI (fast; no runtime)."""
    findings = phi.scan(runtime=False)
    assert findings == [], [f"{f.source}:{f.line} [{f.detector}]" for f in findings]


@pytest.mark.slow
def test_runtime_logs_are_scrubbed_clean():
    """The real proof the rubric asks for: run a broad hermetic slice, capture the live
    JSON log stream, and assert the scrubber left zero PHI on it."""
    findings = phi.scan(runtime=True)
    assert findings == [], [f"{f.source}:{f.line} [{f.detector}]" for f in findings]
