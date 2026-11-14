"""panel_report.py — run the 4-judge clinical panel and write a REPORT.

This CLI is a spot-check quality instrument, NOT a gate. It ALWAYS exits 0 and
is deliberately kept OUT of the pre-push hook / CI chain (that stays
ruff -> pytest -> runner -> gate, deterministic + fast). It writes
`panel_report.json` + a markdown summary.

Modes
-----
--mode ablation (default)   Build real-vs-ablated pairs from the committed
                            `panel_fixtures.json` and run the panel — the demo /
                            submission artifact. Each fixture's Beta is derived
                            by a named ablation engineered so one lens is
                            decisive (`strip_citations`, `drop_abnormal_claim`,
                            `inject_phi_value`, `verbose_preamble`), plus a
                            `safety_split` fixture that forces a 2-2 and
                            exercises the fail-closed tie-breaker.

--compare A.json B.json     Compare two response bundles (or two runner
                            `results.json`). Emits the per-case winner AND the
                            GATE-DISAGREEMENT CROSS-CHECK: every case where the
                            panel's winner disagrees with the deterministic
                            rubric outcome — a meta-check that surfaces either a
                            panel error or a gap in the gate.

--sample N --seed S         Randomly sample N pairs (reproducible via seed)
                            before running — the sampled safety-critical subset.

Call budget
-----------
Hermetic by default: the deterministic stub runs and makes ZERO API calls.
Real judging needs `COPILOT_JUDGE=live` + an Anthropic key. In live mode the
security lens is DETERMINISTIC (0 calls); each pair costs 3 LLM lenses x 2
order-swaps = ~6 Anthropic calls. The 5-fixture ablation run is ~30 live calls;
a full 4x2 sweep over the 65-case gate would be ~390 calls — which is exactly
why this is on-demand and never per-commit.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import sys
from pathlib import Path
from typing import Any, Optional

GOLDEN_DIR = Path(__file__).resolve().parent
COPILOT_DIR = GOLDEN_DIR.parents[1]
if str(COPILOT_DIR) not in sys.path:
    sys.path.insert(0, str(COPILOT_DIR))

from judge_panel import run_panel  # noqa: E402

FIXTURES_FILE = GOLDEN_DIR / "panel_fixtures.json"

_ABNORMAL_MARKERS = ("abnormal", "critical", "urgent", "elevated", "above", "red flag", "flag")
_VERBOSE_PREAMBLE = (
    "Before summarizing, note that this briefing was compiled after a careful and "
    "thorough review of the entire longitudinal record, considering every prior "
    "encounter, all documented medications, and the full history in exhaustive "
    "detail, so that nothing whatsoever could possibly be missed or overlooked, "
    "and only then, at long last, do we arrive at the actual point, which is: "
)


# ---- Ablation transforms (real Alpha -> weaker Beta) -------------------------


def _strip_citations(resp: dict) -> dict:
    """Remove every source_ref — turns cited claims into uncited assertions
    (decisive for rigor + format)."""
    beta = copy.deepcopy(resp)
    for c in beta.get("briefing", []) + beta.get("guideline_evidence", []):
        c["source_refs"] = []
    return beta


def _drop_abnormal_claim(resp: dict) -> dict:
    """Drop a red-flag (abnormal) claim — a dangerous omission (decisive for
    rigor). Targets the first claim whose text names an abnormal finding; falls
    back to the last briefing claim."""
    beta = copy.deepcopy(resp)
    claims = beta.get("briefing", [])
    if not claims:
        return beta
    idx = next(
        (i for i, c in enumerate(claims) if any(m in c["text"].lower() for m in _ABNORMAL_MARKERS)),
        len(claims) - 1,
    )
    claims.pop(idx)
    return beta


def _inject_phi_value(resp: dict, sentinels: list[str]) -> dict:
    """Leak a raw patient value/name into PROSE (decisive for security). Uses a
    real sentinel so the deterministic scan trips exactly as it would on a
    genuine leak."""
    beta = copy.deepcopy(resp)
    name = next((s for s in sentinels if s), "Fernsby")
    leak = f" (patient {name}, most recent value 9.1)"
    beta["notes"] = (beta.get("notes") or "") + leak
    return beta


def _verbose_preamble(resp: dict) -> dict:
    """Bury the answer under a long preamble (decisive for brevity). Adds words
    only — citations and claim count are untouched."""
    beta = copy.deepcopy(resp)
    beta["notes"] = _VERBOSE_PREAMBLE + (beta.get("notes") or "")
    return beta


def _ablate(ablation: str, resp: dict, response_b: Optional[dict], sentinels: list[str]) -> dict:
    if ablation == "safety_split":
        if response_b is None:
            raise ValueError("safety_split fixture must supply an explicit response_b")
        return copy.deepcopy(response_b)
    if ablation == "strip_citations":
        return _strip_citations(resp)
    if ablation == "drop_abnormal_claim":
        return _drop_abnormal_claim(resp)
    if ablation == "inject_phi_value":
        return _inject_phi_value(resp, sentinels)
    if ablation == "verbose_preamble":
        return _verbose_preamble(resp)
    raise ValueError(f"unknown ablation: {ablation}")


ABLATIONS = ("strip_citations", "drop_abnormal_claim", "inject_phi_value", "verbose_preamble", "safety_split")


# ---- Fixture loading + client -------------------------------------------------


def _load_fixtures(path: Path = FIXTURES_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("fixtures", [])


def _maybe_client() -> Any:
    """A live Anthropic client only if COPILOT_JUDGE=live AND a key is present;
    otherwise None so `run_panel` uses the deterministic stub."""
    import os

    if os.environ.get("COPILOT_JUDGE", "stub") != "live":
        return None
    key = os.environ.get("COPILOT_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic

        return anthropic.Anthropic(api_key=key)
    except Exception:  # noqa: BLE001
        return None


# ---- Mode: ablation -----------------------------------------------------------


def run_ablation(sample: Optional[int], seed: int) -> dict:
    fixtures = _load_fixtures()
    if sample is not None:
        rng = random.Random(seed)
        fixtures = rng.sample(fixtures, min(sample, len(fixtures)))
    client = _maybe_client()
    from judge_panel import _load_sentinels

    sentinels = _load_sentinels()

    cases = []
    tally = {"Alpha": 0, "Beta": 0}
    for fx in fixtures:
        alpha = fx["response"]
        beta = _ablate(fx["ablation"], alpha, fx.get("response_b"), sentinels)
        verdict = run_panel(fx.get("user_query", ""), alpha, beta, fx.get("evidence"), client=client)
        decision = verdict["final_clinical_decision"]
        tally[decision["absolute_winner"]] += 1
        cases.append({
            "case_id": fx["case_id"],
            "ablation": fx["ablation"],
            "decisive_lens": fx.get("decisive_lens"),
            "winner": decision["absolute_winner"],
            "vote_split": decision["vote_split"],
            "tie_break": decision["tie_break"],
            "safety_fail": decision.get("safety_fail", False),
            "summary": decision["clinical_utility_summary"],
            "telemetry": verdict["clinical_panel_telemetry"],
        })
    return {
        "mode": "ablation",
        "live": client is not None,
        "n_cases": len(cases),
        "alpha_wins": tally["Alpha"],
        "beta_wins": tally["Beta"],
        "cases": cases,
    }


# ---- Mode: compare ------------------------------------------------------------


def _looks_like_bundle(doc: Any) -> bool:
    if isinstance(doc, list):
        return True
    return isinstance(doc, dict) and "fixtures" in doc


def _bundle_cases(doc: Any) -> dict[str, dict]:
    items = doc if isinstance(doc, list) else doc.get("fixtures", [])
    return {it["case_id"]: it for it in items if it.get("case_id")}


def _rubric_outcome(runner_doc: dict) -> dict[str, str]:
    """case_id -> deterministic pass/fail/skip from a runner results.json."""
    return {c["id"]: c.get("status") for c in runner_doc.get("per_case", []) if c.get("id")}


def run_compare(path_a: Path, path_b: Path, sample: Optional[int], seed: int) -> dict:
    with open(path_a, encoding="utf-8") as f:
        doc_a = json.load(f)
    with open(path_b, encoding="utf-8") as f:
        doc_b = json.load(f)

    client = _maybe_client()
    cases: list[dict] = []
    disagreements: list[dict] = []
    tally = {"Alpha": 0, "Beta": 0}

    if _looks_like_bundle(doc_a) and _looks_like_bundle(doc_b):
        a_cases, b_cases = _bundle_cases(doc_a), _bundle_cases(doc_b)
        shared = sorted(set(a_cases) & set(b_cases))
        if sample is not None:
            shared = random.Random(seed).sample(shared, min(sample, len(shared)))
        for cid in shared:
            a, b = a_cases[cid], b_cases[cid]
            verdict = run_panel(a.get("user_query", ""), a["response"], b["response"], a.get("evidence"), client=client)
            decision = verdict["final_clinical_decision"]
            tally[decision["absolute_winner"]] += 1
            # Deterministic rubric outcome per side, if the bundle carries one.
            ra, rb = a.get("rubric_outcome"), b.get("rubric_outcome")
            rubric_winner = _rubric_winner(ra, rb)
            entry = {
                "case_id": cid,
                "winner": decision["absolute_winner"],
                "vote_split": decision["vote_split"],
                "tie_break": decision["tie_break"],
                "rubric_winner": rubric_winner,
            }
            cases.append(entry)
            if rubric_winner in ("Alpha", "Beta") and rubric_winner != decision["absolute_winner"]:
                disagreements.append(entry)
    else:
        # Two runner results.json — no responses to judge; report the
        # deterministic rubric agreement only (panel judging needs response
        # bundles). The cross-check is empty by construction here.
        oa, ob = _rubric_outcome(doc_a), _rubric_outcome(doc_b)
        for cid in sorted(set(oa) & set(ob)):
            cases.append({
                "case_id": cid,
                "rubric_a": oa[cid],
                "rubric_b": ob[cid],
                "note": "panel judging skipped — runner results carry no responses",
            })

    return {
        "mode": "compare",
        "live": client is not None,
        "n_cases": len(cases),
        "alpha_wins": tally["Alpha"],
        "beta_wins": tally["Beta"],
        "gate_disagreements": disagreements,
        "cases": cases,
    }


def _rubric_winner(ra: Optional[str], rb: Optional[str]) -> Optional[str]:
    if ra == "pass" and rb != "pass":
        return "Alpha"
    if rb == "pass" and ra != "pass":
        return "Beta"
    return "tie" if (ra or rb) else None


# ---- Markdown ----------------------------------------------------------------


def to_markdown(report: dict) -> str:
    lines = [
        "# Clinical eval panel report (spot-check, NON-GATING)",
        "",
        f"- mode: `{report['mode']}`  |  live: `{report['live']}`  |  cases: {report['n_cases']}",
        f"- A/B tally: Alpha {report.get('alpha_wins', 0)} / Beta {report.get('beta_wins', 0)}",
        "",
        "| case | ablation / rubric | winner | vote split | tie-break |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in report.get("cases", []):
        second = c.get("ablation") or f"{c.get('rubric_a', '-')}/{c.get('rubric_b', '-')}"
        winner = c.get("winner", "-")
        # vote_split is a string ("3-1" / "2-2 via fail_closed_default").
        vs_s = c.get("vote_split") or "-"
        sf = " ⚠SAFETY_FAIL" if c.get("safety_fail") else ""
        lines.append(f"| {c['case_id']} | {second} | {winner}{sf} | {vs_s} | {c.get('tie_break', '-')} |")
    dis = report.get("gate_disagreements") or []
    if dis:
        lines += ["", "## Gate-disagreement cross-check (panel vs deterministic rubric)"]
        for d in dis:
            lines.append(f"- {d['case_id']}: panel={d['winner']} but rubric favored {d['rubric_winner']}")
    elif report["mode"] == "compare":
        lines += ["", "## Gate-disagreement cross-check", "- none (panel agreed with every scored rubric outcome)"]
    lines += ["", "_This report never blocks a push; the deterministic 65-case gate is authoritative._"]
    return "\n".join(lines)


# ---- main --------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--mode", choices=["ablation", "compare"], default="ablation")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"),
                    help="two response bundles or runner results.json (implies --mode compare)")
    ap.add_argument("--sample", type=int, default=None, help="sample N pairs (reproducible with --seed)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=str(GOLDEN_DIR / "panel_report.json"))
    ap.add_argument("--out-md", default=str(GOLDEN_DIR / "panel_report.md"))
    args = ap.parse_args(argv)

    if args.compare:
        report = run_compare(Path(args.compare[0]), Path(args.compare[1]), args.sample, args.seed)
    else:
        report = run_ablation(args.sample, args.seed)

    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = to_markdown(report)
    Path(args.out_md).write_text(md + "\n", encoding="utf-8")
    print(md)
    print(f"\nwrote {args.out} and {args.out_md}")
    # A REPORT never gates — always exit 0.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
