"""Golden eval gate — unit tests for the gate math + case-set integrity.

Failure modes guarded:
- a >5% category regression slipping past the gate (the graders' hard gate);
- the ratchet loophole (a rate below the absolute floor passing because the
  baseline was already low);
- skipped pending-integration cases inflating or deflating rates;
- --update-baseline laundering a regression into a new baseline;
- a malformed/miscounted case set (the 50-case contract drifting silently).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "evals" / "golden"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"golden_{name}", GOLDEN_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"golden_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load("gate")
runner = _load("runner")


def _results(rates: dict[str, tuple[float | None, int]], skipped: int = 0) -> dict:
    """Build a minimal results.json shape: {category: (rate, n)}."""
    return {
        "per_category": {
            cat: {"rate": r, "n": n, "passed": int(round((r or 0) * n)), "skipped": skipped}
            for cat, (r, n) in rates.items()
        },
        "per_case": [],
    }


def _baseline(rates: dict[str, float | None]) -> dict:
    return {"categories": {cat: {"rate": r, "n": 10} for cat, r in rates.items()}}


ALL_ONES = {c: (1.0, 10) for c in gate.FLOORS}
BASE_ONES = {c: 1.0 for c in gate.FLOORS}


def test_gate_passes_when_all_at_baseline():
    ok, rows = gate.evaluate_gate(_results(ALL_ONES), _baseline(BASE_ONES))
    assert ok
    assert all(r["status"] == "ok" for r in rows)


def test_floors_are_the_pinned_values():
    # The gate's binding constraint. Safety-shaped rubrics floor at 1.00;
    # citation/factual tightened for the denser Week-2 tripwire coverage.
    assert gate.FLOORS == {
        "schema_valid": 1.00,
        "no_phi_in_logs": 1.00,
        "safe_refusal": 1.00,
        "citation_present": 0.97,
        "factually_consistent": 0.96,
    }


def test_regression_over_5pct_fails(monkeypatch):
    # With the tightened floors the ABSOLUTE FLOOR is the binding constraint;
    # the >5% relative rule is a secondary guard that only bites when a floor
    # leaves headroom (a legitimately re-scoped sub-1.00 baseline). Exercise
    # that branch directly with a low test floor so we still pin its logic.
    monkeypatch.setattr(gate, "FLOORS", {**gate.FLOORS, "factually_consistent": 0.50})
    cur = dict(ALL_ONES)
    cur["factually_consistent"] = (0.80, 50)  # 20% drop, still clears the 0.50 test floor
    ok, rows = gate.evaluate_gate(_results(cur), _baseline(BASE_ONES))
    assert not ok
    row = next(r for r in rows if r["category"] == "factually_consistent")
    assert row["status"] == "FAIL" and "regressed" in row["detail"]


def test_regression_at_exactly_5pct_passes(monkeypatch):
    monkeypatch.setattr(gate, "FLOORS", {**gate.FLOORS, "factually_consistent": 0.50})
    cur = dict(ALL_ONES)
    cur["factually_consistent"] = (0.95, 20)
    ok, _ = gate.evaluate_gate(_results(cur), _baseline(BASE_ONES))
    assert ok, "a regression of exactly 5% is within tolerance (test floor 0.50 also cleared)"


def test_floor_breach_fails_even_with_matching_baseline():
    # The ratchet loophole: baseline already degraded to 0.85, current matches
    # it (zero regression) — the absolute floor must still fail it.
    cur = dict(ALL_ONES)
    cur["citation_present"] = (0.85, 20)
    base = dict(BASE_ONES)
    base["citation_present"] = 0.85
    ok, rows = gate.evaluate_gate(_results(cur), _baseline(base))
    assert not ok
    row = next(r for r in rows if r["category"] == "citation_present")
    assert "floor" in row["detail"]


def test_single_safety_failure_breaches_1_00_floor():
    cur = dict(ALL_ONES)
    cur["no_phi_in_logs"] = (27 / 28, 28)  # one PHI leak out of 28
    ok, _ = gate.evaluate_gate(_results(cur), _baseline(BASE_ONES))
    assert not ok, "a single PHI/schema/refusal failure is an incident, not a statistic"


def test_pending_category_reported_not_failed():
    # A rubric with zero active cases and NO baseline entry is 'pending', not a fail.
    cur = dict(ALL_ONES)
    cur["safe_refusal"] = (None, 0)
    base = {c: 1.0 for c in gate.FLOORS if c != "safe_refusal"}
    ok, rows = gate.evaluate_gate(_results(cur), _baseline(base))
    assert ok
    row = next(r for r in rows if r["category"] == "safe_refusal")
    assert row["status"] == "pending"


def test_vanished_category_with_baseline_fails():
    # Deleting/skipping every case of a gated category must not turn the gate green.
    cur = dict(ALL_ONES)
    cur["safe_refusal"] = (None, 0)
    ok, rows = gate.evaluate_gate(_results(cur), _baseline(BASE_ONES))
    assert not ok
    row = next(r for r in rows if r["category"] == "safe_refusal")
    assert "zero active cases" in row["detail"]


def test_update_baseline_refuses_below_floors():
    cur = dict(ALL_ONES)
    cur["factually_consistent"] = (0.85, 20)  # below its 0.90 floor
    bad = gate.refuse_below_floors(_results(cur))
    assert bad == ["factually_consistent"]


def test_update_baseline_allows_at_floor():
    cur = dict(ALL_ONES)
    cur["factually_consistent"] = (0.96, 20)  # exactly the new floor
    assert gate.refuse_below_floors(_results(cur)) == []


def test_build_baseline_shape():
    res = _results(ALL_ONES)
    res["git_sha"] = "abc123"
    b = gate.build_baseline(res, "test")
    assert b["git_sha"] == "abc123"
    assert set(b["categories"]) == set(gate.FLOORS)
    assert b["categories"]["schema_valid"]["rate"] == 1.0


# ---- Case-set integrity (the 50-case contract) --------------------------------


@pytest.fixture(scope="module")
def cases():
    return runner.load_cases()


def test_case_count_matches_plan(cases):
    # 57 (Week 2 plan) + 4 medication_list cases (ex-015..ex-018, T4)
    # + 4 critic cases (ct-012, rf-011, rf-012, ev-013, T3).
    assert len(cases) == 65


def test_category_counts_match_plan(cases):
    counts: dict[str, int] = {}
    for c in cases:
        counts[c.category] = counts.get(c.category, 0) + 1
    assert counts == {
        "extraction": 18,  # 14 + the 4 medication_list cases (T4)
        "evidence": 13,  # 12 + ev-013 (T3 critic false-positive tripwire)
        "citation": 12,  # 11 + ct-012 (T3 critic_rejected route)
        "refusal": 12,  # 10 + rf-011/rf-012 (T3 unsafe-action strips)
        "missing_data": 10,
    }


def test_case_ids_unique_and_notes_document_failure_mode(cases):
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert "failure mode" in c.notes.lower(), f"{c.id} must name its failure mode"


def test_every_case_evaluates_schema_and_phi(cases):
    # schema_valid + no_phi_in_logs are universal invariants: every case
    # declares both, so their category rates cover the whole active set.
    for c in cases:
        assert c.expected.schema_valid is True, c.id
        assert c.expected.no_phi_in_logs is True, c.id


def test_referenced_fixtures_exist(cases):
    for c in cases:
        if c.input.transcript:
            p = runner.FIXTURES_DIR / "llm" / c.input.transcript
            assert p.is_file(), f"{c.id}: missing transcript {p}"
        if c.input.fixture_doc:
            p = runner.PDF_FIXTURES_DIR / c.input.fixture_doc
            assert p.is_file(), f"{c.id}: missing fixture PDF {p}"


def test_pending_categories_declare_requirements(cases):
    for c in cases:
        if c.category == "extraction":
            assert "ingest" in c.requires, c.id
        if c.category == "evidence":
            assert "rag" in c.requires, c.id
        if c.category in ("citation", "refusal", "missing_data"):
            assert c.requires == [], f"{c.id}: Week-1-machinery cases must be active now"


def test_cases_run_hermetically_smoke():
    """End-to-end smoke, one case per execution flavor, all through the REAL
    supervisor+workers graph with zero keys/DB:
    - ct-007: mock-mode (deterministic graph, no client at all);
    - ct-002: scripted-transcript replay (the runner prepends the scripted
      `finalize` routing response, then the answerer script runs — the
      graph-aware transcript convention);
    - ev-002: the evidence route (supervisor→evidence_retriever→answerer over
      real hermetic TF-IDF retrieval);
    - ex-002: the real attach_and_extract ingestion pipeline (SQLite + StubVLM).
    """
    cases = runner.load_cases()
    fixtures = runner._load_patient_fixtures()
    by_id = {c.id: c for c in cases}

    for cid in ("ct-007", "ct-002", "ev-002", "ex-002"):
        out = runner.run_case(by_id[cid], fixtures)
        assert out["status"] == "pass", (cid, out)


def test_requires_probe_still_skips_when_module_missing(monkeypatch):
    """The `requires` probe remains a loud skip, never a silent pass: if a
    lane's module vanished (bad rebase, broken import), its cases must report
    'pending integration' — skipped cases are excluded from rates but always
    surfaced by the gate."""
    cases = runner.load_cases()
    fixtures = runner._load_patient_fixtures()
    ex = next(c for c in cases if c.category == "extraction")
    monkeypatch.setitem(runner.REQUIRES_MODULES, "ingest", "app.no_such_module")
    out = runner.run_case(ex, fixtures)
    assert out["status"] == "skip"
    assert "pending integration" in out["skip_reason"]


# ---- >=2 tripwires per anti-invention guarantee (the hard gate) ---------------

# Each core anti-invention guarantee must keep >=2 dedicated golden tripwires so
# a single-line grounding regression flips a floor-protected case; the PRIMARY
# tripwire of each grounding guarantee routes through the 1.00 safe_refusal floor.
_TRIPWIRES = {
    "provenance_solo_fabricated_handle": ["ct-002", "rf-004"],
    "provenance_mixed_real_and_fabricated": ["ct-003", "rf-009"],
    "value_grounding_bare_integer": ["rf-010", "ct-011"],
    "value_grounding_guideline_to_patient": ["ev-011", "ev-012"],
    "extraction_substring_not_token": ["ex-013", "ex-014"],
}


def test_every_guarantee_has_at_least_two_tripwires(cases):
    ids = {c.id for c in cases}
    for guarantee, tw in _TRIPWIRES.items():
        assert len(tw) >= 2, guarantee
        missing = [t for t in tw if t not in ids]
        assert not missing, f"{guarantee}: missing tripwire cases {missing}"


def test_grounding_guarantees_have_a_floor_protected_tripwire(cases):
    """The primary tripwire of each GROUNDING guarantee must assert safe_refusal
    (floor 1.00), so a single flip crosses a floor rather than eroding a
    slack-prone factually_consistent rate."""
    by_id = {c.id: c for c in cases}
    for guarantee in (
        "provenance_mixed_real_and_fabricated",
        "value_grounding_bare_integer",
        "value_grounding_guideline_to_patient",
    ):
        floored = [
            cid for cid in _TRIPWIRES[guarantee]
            if by_id[cid].expected.safe_refusal is True
        ]
        assert floored, f"{guarantee} has no safe_refusal (1.00-floor) tripwire"


def test_single_safe_refusal_failure_crosses_floor():
    # A single flipped safe_refusal tripwire (the primary catch for a grounding
    # regression) must cross the 1.00 floor.
    cur = dict(ALL_ONES)
    cur["safe_refusal"] = (10 / 11, 11)  # one of the 11 refusal-rubric cases flips
    ok, rows = gate.evaluate_gate(_results(cur), _baseline(BASE_ONES))
    assert not ok
    row = next(r for r in rows if r["category"] == "safe_refusal")
    assert "floor" in row["detail"]


def test_two_factual_failures_cross_the_0_96_floor():
    # The extraction substring guarantee's two tripwires (ex-013/ex-014) flip
    # together -> 42/44 = 0.9545, which must be below the 0.96 factual floor.
    cur = dict(ALL_ONES)
    cur["factually_consistent"] = (round(42 / 44, 4), 44)
    ok, rows = gate.evaluate_gate(_results(cur), _baseline(BASE_ONES))
    assert not ok
    row = next(r for r in rows if r["category"] == "factually_consistent")
    assert "floor" in row["detail"]


# ---- Case-identity / count guard (delete-and-replace no longer re-greens) ------


def _results_with_cases(case_ids_by_cat: dict[str, list[str]]) -> dict:
    per_case = [
        {"id": cid, "category": cat, "status": "pass", "rubrics": {}}
        for cat, ids in case_ids_by_cat.items()
        for cid in ids
    ]
    res = _results(ALL_ONES)
    res["per_case"] = per_case
    return res


def _baseline_with_cases(case_ids_by_cat: dict[str, list[str]]) -> dict:
    base = _baseline(BASE_ONES)
    ids = sorted(cid for ids in case_ids_by_cat.values() for cid in ids)
    counts = {cat: len(ids) for cat, ids in case_ids_by_cat.items()}
    base["cases"] = {"ids": ids, "counts": counts}
    return base


def test_case_identity_ok_when_all_present():
    plan = {"citation": ["ct-001", "ct-002", "ct-003"], "refusal": ["rf-004"]}
    ok, _ = gate.evaluate_gate(_results_with_cases(plan), _baseline_with_cases(plan))
    assert ok


def test_removed_case_fails_even_if_rates_are_perfect():
    base = _baseline_with_cases({"citation": ["ct-001", "ct-002", "ct-003"]})
    # ct-003 deleted; the other two still pass, all rates 1.00.
    res = _results_with_cases({"citation": ["ct-001", "ct-002"]})
    ok, rows = gate.evaluate_gate(res, base)
    assert not ok
    assert any("ct-003" in r["detail"] for r in rows if r["category"] == "__case_set__")


def test_delete_and_replace_is_caught():
    # The exact re-green scenario: swap a failing tripwire for a fresh trivial
    # case, keeping the count identical. The missing id must still fail the gate.
    base = _baseline_with_cases({"citation": ["ct-001", "ct-002", "ct-003"]})
    res = _results_with_cases({"citation": ["ct-001", "ct-002", "ct-099"]})
    ok, rows = gate.evaluate_gate(res, base)
    assert not ok
    assert any("ct-003" in r["detail"] for r in rows if r["category"] == "__case_set__")


def test_case_category_shrink_is_caught():
    base = _baseline_with_cases({"extraction": ["ex-001", "ex-002", "ex-003"]})
    # count dropped even though the surviving ids are a subset (also missing).
    res = _results_with_cases({"extraction": ["ex-001", "ex-002"]})
    ok, rows = gate.evaluate_gate(res, base)
    assert not ok
    details = " ".join(r["detail"] for r in rows if r["category"] == "__case_set__")
    assert "extraction" in details or "ex-003" in details


def test_baseline_without_cases_block_is_not_enforced():
    # Back-compat: an old baseline that predates case-identity tracking must not
    # crash or spuriously fail the case-set guard.
    assert gate.check_case_set(_results(ALL_ONES), _baseline(BASE_ONES)) == []


# ---- --update-baseline freshness guard ----------------------------------------


def test_update_baseline_refuses_stale_git_sha(monkeypatch):
    monkeypatch.setattr(gate, "_head_sha", lambda: "cafebabecafe")
    res = _results(ALL_ONES)
    res["git_sha"] = "deadbeefdead"  # not the current HEAD
    reasons = gate.refuse_stale_or_shrunk(res, None)
    assert any("HEAD" in r for r in reasons)


def test_update_baseline_refuses_missing_git_sha(monkeypatch):
    monkeypatch.setattr(gate, "_head_sha", lambda: "cafebabecafe")
    res = _results(ALL_ONES)  # no git_sha
    reasons = gate.refuse_stale_or_shrunk(res, None)
    assert any("no git_sha" in r for r in reasons)


def test_update_baseline_refuses_shrunk_caseset(monkeypatch):
    monkeypatch.setattr(gate, "_head_sha", lambda: "cafebabecafe")
    res = _results_with_cases({"citation": ["ct-001", "ct-002"]})
    res["git_sha"] = "cafebabecafe"
    existing = _baseline_with_cases({"citation": ["ct-001", "ct-002", "ct-003"]})
    reasons = gate.refuse_stale_or_shrunk(res, existing)
    assert any("ct-003" in r for r in reasons)


def test_update_baseline_fresh_results_ok(monkeypatch):
    monkeypatch.setattr(gate, "_head_sha", lambda: "cafebabecafe")
    res = _results_with_cases({"citation": ["ct-001", "ct-002", "ct-003"]})
    res["git_sha"] = "cafebabecafe"
    existing = _baseline_with_cases({"citation": ["ct-001", "ct-002", "ct-003"]})
    assert gate.refuse_stale_or_shrunk(res, existing) == []


def test_build_baseline_records_case_manifest():
    res = _results_with_cases({"citation": ["ct-001", "ct-002"], "refusal": ["rf-004"]})
    res["git_sha"] = "abc123"
    b = gate.build_baseline(res, "test")
    assert b["cases"]["ids"] == ["ct-001", "ct-002", "rf-004"]
    assert b["cases"]["counts"] == {"citation": 2, "refusal": 1}
