"""Deterministic core tests for the 4-judge clinical eval panel.

The aggregation is the load-bearing logic. THREE lenses (rigor/brevity/format)
cast comparative votes; `security` is a DETERMINISTIC GATE (PHI scan), and an
uncited claim is an equal deterministic danger. The safety facts are computed,
never judged — so an adversarial LLM lens can never vote danger into "safe".

Every test pins one branch, and the safety guards (a leaking / fabricating /
red-flag-omitting Beta must NEVER be crowned over a clean Alpha) are the reason
the panel is trustworthy at all — they encode the defects an earlier version
shipped (a PHI-leaker and a red-flag-omitter could win).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "evals" / "golden"
if str(GOLDEN_DIR) not in sys.path:
    sys.path.insert(0, str(GOLDEN_DIR))

import judge_panel  # noqa: E402
import panel_report  # noqa: E402
from judge_panel import run_panel  # noqa: E402

MARK_A = "__physalpha__"
MARK_B = "__physbeta__"
SENTINEL = "Fernsby"  # a real fixture PHI sentinel (a patient surname)


# ---- Thread-safe scripted client ---------------------------------------------


class ScriptedPanelClient:
    """Keys each forced-tool call by lens so it is robust to the ThreadPool
    fan-out. `plan[lens]` is 'A' | 'B' (vote that PHYSICAL response consistently)
    or 'biasAlpha' (always return LABEL 'Alpha' → physical winner flips across
    orders → inconsistent). Rigor may be ('A'|'B', unsupported_claim_str)."""

    def __init__(self, plan: dict):
        self.plan = plan
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, *, system, messages, tools, **kw):
        lens = system.splitlines()[0].split("LENS_ID:")[1].strip()
        content = messages[0]["content"]
        alpha_seg = content.split("RESPONSE ALPHA")[1].split("RESPONSE BETA")[0]
        physical_alpha = "A" if MARK_A in alpha_seg else "B"
        spec = self.plan.get(lens, "A")
        unsupported = None
        if isinstance(spec, tuple):
            spec, unsupported = spec
        label = "Alpha" if spec == "biasAlpha" else ("Alpha" if physical_alpha == spec else "Beta")
        inp = {"winner": label, "rationale": f"scripted {lens} vote"}
        if lens == "rigor":
            inp["unsupported_claim"] = unsupported
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="cast_vote", input=inp)])


def make_response(marker: str, *, cited: int = 2, uncited: int = 0,
                  quarantined: int = 0, extra_prose: str = "") -> dict:
    briefing = [
        {"text": f"cited change {i} {marker}",
         "source_refs": [{"source_type": "patient_record", "table": "procedure_result",
                          "primary_key": 700 + i, "label": "lab row"}]}
        for i in range(cited)
    ]
    briefing += [{"text": f"uncited assertion {j} {marker}", "source_refs": []} for j in range(uncited)]
    return {
        "correlation_id": "t",
        "briefing": briefing,
        "quarantined_count": quarantined,
        "notes": f"{marker} summary {extra_prose}".strip(),
    }


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("COPILOT_JUDGE", "live")


def _run(plan, resp_a, resp_b, evidence=None):
    return run_panel("what changed?", resp_a, resp_b, evidence or [], client=ScriptedPanelClient(plan))


def _judge(out, lens):
    return next(t for t in out["clinical_panel_telemetry"] if t["judge_id"] == f"{lens}_judge")


def _forced(out, lens):
    return _judge(out, lens)["economic_choice"]["forced_winner"]


# ---- Quality majority (both responses deterministically SAFE) ----------------


def test_quality_majority_all_agree(live_env):
    # Both clean (no leak, no uncited); 3 quality lenses vote A → 3-0 Alpha.
    a, b = make_response(MARK_A), make_response(MARK_B)
    out = _run({"rigor": "A", "brevity": "A", "format": "A"}, a, b)
    dec = out["final_clinical_decision"]
    assert dec["absolute_winner"] == "Alpha"
    assert dec["vote_split"] == "3-0"
    assert dec["tie_break"] == "none"
    assert dec["safety_fail"] is False


def test_quality_majority_2_1(live_env):
    a, b = make_response(MARK_A), make_response(MARK_B)
    out = _run({"rigor": "A", "brevity": "A", "format": "B"}, a, b)
    dec = out["final_clinical_decision"]
    assert dec["absolute_winner"] == "Alpha"
    assert dec["vote_split"] == "2-1"


def test_quality_tie_broken_by_rigor(live_env):
    # 1-1 quality tie (rigor=A, brevity=B, format abstains via bias), both safe →
    # rigor breaks it.
    a, b = make_response(MARK_A), make_response(MARK_B)
    out = _run({"rigor": "A", "brevity": "B", "format": "biasAlpha"}, a, b)
    dec = out["final_clinical_decision"]
    assert _forced(out, "format") == "inconsistent"
    assert dec["tie_break"] == "rigor_tiebreak"
    assert dec["absolute_winner"] == "Alpha"


# ---- Deterministic safety veto (the trust-critical guards) -------------------


def test_phi_leaking_beta_can_never_win(live_env):
    # GUARD (V-D1/D2): Beta leaks a PHI sentinel; even if EVERY quality lens is
    # (adversarially) scripted to prefer Beta, the clean Alpha wins by hard veto.
    a = make_response(MARK_A)
    b = make_response(MARK_B, extra_prose=SENTINEL)
    out = _run({"rigor": "B", "brevity": "B", "format": "B"}, a, b)
    dec = out["final_clinical_decision"]
    assert dec["absolute_winner"] == "Alpha", "a PHI-leaking response must never be crowned"
    assert dec["tie_break"] == "safety_veto"
    assert dec["safety_fail"] is False


def test_fabricating_beta_can_never_win(live_env):
    # GUARD: Beta carries an uncited claim (fabrication); all lenses prefer Beta;
    # clean Alpha still wins by the uncited veto.
    a = make_response(MARK_A)
    b = make_response(MARK_B, cited=1, uncited=1)
    out = _run({"rigor": "B", "brevity": "B", "format": "B"}, a, b)
    dec = out["final_clinical_decision"]
    assert dec["absolute_winner"] == "Alpha"
    assert dec["tie_break"] == "safety_veto"


def test_both_unsafe_flags_safety_fail(live_env):
    # GUARD (V-D2): Alpha leaks PHI, Beta fabricates → NEITHER is safe. The panel
    # must flag safety_fail and never present a winner as clean.
    a = make_response(MARK_A, extra_prose=SENTINEL)
    b = make_response(MARK_B, cited=1, uncited=1)
    out = _run({"rigor": "A", "brevity": "A", "format": "A"}, a, b)
    dec = out["final_clinical_decision"]
    assert dec["safety_fail"] is True
    assert dec["tie_break"] == "both_unsafe"
    assert "SAFETY FAIL" in dec["clinical_utility_summary"]


def test_red_flag_omission_never_wins_as_withholding(live_env):
    # GUARD (V-D3): the exact regression that shipped. Alpha surfaces 3 grounded
    # findings; Beta drops one (a red-flag omission) and is "more withholding"
    # (higher quarantined_count). Both are deterministically SAFE (no leak, no
    # uncited), the quality lenses tie 1-1, rigor abstains → fail-closed default
    # must pick the MORE-GROUNDED Alpha, never reward the omitter.
    a = make_response(MARK_A, cited=3, quarantined=0)
    b = make_response(MARK_B, cited=1, quarantined=2)
    out = _run({"rigor": "biasAlpha", "brevity": "B", "format": "A"}, a, b)
    dec = out["final_clinical_decision"]
    assert dec["tie_break"] == "fail_closed_default"
    assert dec["absolute_winner"] == "Alpha", "omitting a red flag must never win as 'more withholding'"


# ---- Stub determinism --------------------------------------------------------


def test_stub_is_deterministic_and_hermetic():
    a = make_response(MARK_A, cited=3)
    b = make_response(MARK_B, cited=1, uncited=2)
    o1, o2 = run_panel("q", a, b), run_panel("q", a, b)
    assert o1 == o2
    # Beta has an uncited claim → unsafe → Alpha wins by veto, deterministically.
    assert o1["final_clinical_decision"]["absolute_winner"] == "Alpha"


def test_stub_used_when_live_env_but_no_client(monkeypatch):
    monkeypatch.setenv("COPILOT_JUDGE", "live")
    a, b = make_response(MARK_A, cited=3), make_response(MARK_B, cited=1)
    out = run_panel("q", a, b, client=None)  # no client → stub even if env live
    assert out["final_clinical_decision"]["absolute_winner"] == "Alpha"


# ---- Schema conformance (the user's shape) -----------------------------------


def test_output_schema_matches_user_shape(live_env):
    a, b = make_response(MARK_A), make_response(MARK_B)
    out = _run({"rigor": "A", "brevity": "A", "format": "A"}, a, b)
    assert set(out) == {"clinical_panel_telemetry", "final_clinical_decision"}
    tel = out["clinical_panel_telemetry"]
    assert [t["judge_id"] for t in tel] == ["rigor_judge", "brevity_judge", "format_judge", "security_judge"]
    for t in tel:
        assert set(t) >= {"judge_id", "telemetry", "economic_choice"}
        tm = t["telemetry"]
        assert {"response_alpha_word_count", "response_beta_word_count",
                "response_alpha_has_medical_hallucination",
                "response_beta_has_medical_hallucination"} <= set(tm)
        assert all(isinstance(tm[k], int) for k in (
            "response_alpha_word_count", "response_beta_word_count",
            "response_alpha_has_medical_hallucination", "response_beta_has_medical_hallucination"))
        ec = t["economic_choice"]
        assert set(ec) >= {"forced_winner", "marginal_utility_rationale"}
        assert ec["forced_winner"] in ("Alpha", "Beta", "abstain", "inconsistent")
    dec = out["final_clinical_decision"]
    assert set(dec) == {"absolute_winner", "vote_split", "clinical_utility_summary", "tie_break", "safety_fail"}
    assert isinstance(dec["vote_split"], str)


def test_word_counts_and_halluc_bits_computed_in_code(live_env):
    a = make_response(MARK_A, cited=4)
    b = make_response(MARK_B, cited=1, uncited=1)  # B fabricates
    out = _run({"rigor": "A", "brevity": "A", "format": "A"}, a, b)
    tm = _judge(out, "rigor")["telemetry"]
    assert tm["response_alpha_word_count"] > tm["response_beta_word_count"]
    assert tm["response_alpha_word_count"] == judge_panel._word_count(judge_panel._as_response(a))
    # deterministic hallucination bit: identical across ALL judges, from _has_uncited.
    for lens in ("rigor", "brevity", "format", "security"):
        t = _judge(out, lens)["telemetry"]
        assert t["response_alpha_has_medical_hallucination"] == 0
        assert t["response_beta_has_medical_hallucination"] == 1


# ---- PHI scrub ---------------------------------------------------------------


def test_phi_sentinel_and_values_never_in_output(live_env):
    # Beta leaks a sentinel + a value into prose; a scripted rigor rationale
    # echoes both. Neither the name nor the value may survive into the output.
    a = make_response(MARK_A)
    b = make_response(MARK_B, extra_prose=f"{SENTINEL} potassium 6.2 mmol/L on 2026-07-01")

    class LeakyRigor(ScriptedPanelClient):
        def _create(self, *, system, messages, tools, **kw):
            resp = super()._create(system=system, messages=messages, tools=tools, **kw)
            if "LENS_ID: rigor" in system:
                resp.content[0].input["rationale"] = f"Beta leaked {SENTINEL} and potassium 6.2 mmol/L on 2026-07-01"
                resp.content[0].input["unsupported_claim"] = "value 6.2 mmol/L"
            return resp

    out = run_panel("q", a, b, [], client=LeakyRigor({"rigor": "A", "brevity": "A", "format": "A"}))
    blob = json.dumps(out)
    assert SENTINEL not in blob
    assert "6.2 mmol/L" not in blob  # value-shaped redaction
    assert "2026-07-01" not in blob  # date redaction
    sec = _judge(out, "security")["telemetry"]
    assert sec["response_beta_phi_leaks"] >= 1 and sec["response_alpha_phi_leaks"] == 0


def test_security_scan_ignores_structured_labels(live_env):
    # A drug named only in a structured LABEL (not prose) is attribution, not a leak.
    a = {
        "correlation_id": "t", "quarantined_count": 0,
        "briefing": [{"text": f"an active medication is documented (med:0) {MARK_A}",
                      "source_refs": [{"source_type": "patient_record", "table": "prescriptions",
                                       "primary_key": 1, "label": "Metformin 1000 mg"}]}],
        "notes": "",
    }
    b = make_response(MARK_B)
    out = _run({"rigor": "A", "brevity": "A", "format": "A"}, a, b)
    assert _judge(out, "security")["telemetry"]["response_alpha_phi_leaks"] == 0


# ---- Ablation transforms -> intended decisive outcome ------------------------


def _fixture(case_id):
    for fx in panel_report._load_fixtures():
        if fx["case_id"] == case_id:
            return fx
    raise KeyError(case_id)


def test_each_ablation_prefers_the_real_alpha():
    # The whole demo: every real-vs-ablated pair must prefer the real Alpha.
    for case_id, transform in [
        ("pf-strip-citations", lambda fx: panel_report._strip_citations(fx["response"])),
        ("pf-drop-abnormal", lambda fx: panel_report._drop_abnormal_claim(fx["response"])),
        ("pf-inject-phi", lambda fx: panel_report._inject_phi_value(fx["response"], judge_panel._load_sentinels())),
        ("pf-verbose-preamble", lambda fx: panel_report._verbose_preamble(fx["response"])),
    ]:
        fx = _fixture(case_id)
        beta = transform(fx)
        out = run_panel(fx["user_query"], fx["response"], beta, fx["evidence"])
        assert out["final_clinical_decision"]["absolute_winner"] == "Alpha", case_id


def test_inject_phi_wins_by_deterministic_veto():
    fx = _fixture("pf-inject-phi")
    beta = panel_report._inject_phi_value(fx["response"], judge_panel._load_sentinels())
    out = run_panel(fx["user_query"], fx["response"], beta, fx["evidence"])
    assert out["final_clinical_decision"]["tie_break"] == "safety_veto"
    assert _judge(out, "security")["telemetry"]["response_beta_phi_leaks"] >= 1


def test_safety_split_fixture_rejects_the_omitter():
    # The regression fixture: Beta downplays a red flag with an uncited claim →
    # unsafe → the complete Alpha wins (previously wrongly picked Beta).
    fx = _fixture("pf-safety-split")
    out = run_panel(fx["user_query"], fx["response"], fx["response_b"], fx["evidence"])
    assert out["final_clinical_decision"]["absolute_winner"] == "Alpha"


# ---- CLI is hermetic + non-gating --------------------------------------------


def test_panel_report_ablation_exits_zero(tmp_path):
    out = tmp_path / "panel_report.json"
    rc = panel_report.main(["--mode", "ablation", "--out", str(out), "--out-md", str(tmp_path / "r.md")])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["live"] is False and report["n_cases"] == 5
    assert report["alpha_wins"] == 5 and report["beta_wins"] == 0


def test_panel_report_sample_is_reproducible(tmp_path):
    out = tmp_path / "r.json"
    rc = panel_report.main(["--mode", "ablation", "--sample", "2", "--seed", "7",
                            "--out", str(out), "--out-md", str(tmp_path / "r.md")])
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["n_cases"] == 2


def test_non_gating_gate_files_untouched():
    # Proof the panel never altered the deterministic gate: after this module's
    # panel runs, gate.py/baseline.json carry no UNCOMMITTED modification.
    # (Diff vs HEAD, not a pinned SHA — a legitimate, committed
    # `gate.py --update-baseline` re-pin must not redden this test; the thing
    # the panel must never do is mutate the gate files at runtime.)
    import subprocess
    r = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--",
         "copilot/evals/golden/gate.py", "copilot/evals/golden/baseline.json"],
        cwd=str(GOLDEN_DIR.parents[2]), capture_output=True, text=True,
    )
    assert r.stdout.strip() == "", f"panel must not touch the gate; changed: {r.stdout}"
