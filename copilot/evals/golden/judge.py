"""LLM judge tier — OPTIONAL, never part of the gate.

Default (`COPILOT_JUDGE=stub`, always in hermetic mode): a deterministic stub
verdict. The five gate rubrics are all deterministic (schema parse, ref
presence, value-set math, refusal shape, sentinel scan) — the gate never
depends on a model's mood (W2_ARCHITECTURE.md §8.2).

Live tier (runner `--live` AND `COPILOT_JUDGE=live`): one Anthropic call with a
FORCED boolean tool — used only for judged spot-checks of prose quality, never
for pass/fail authority.
"""

from __future__ import annotations

import os
from typing import Any

JUDGE_TOOL = {
    "name": "judge_verdict",
    "description": "Return a boolean verdict on whether the briefing is faithful to the cited evidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "consistent": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["consistent", "reason"],
    },
}


def judge_case(case_id: str, briefing_text: str, evidence_text: str, live: bool = False) -> dict[str, Any]:
    """Boolean spot-check verdict. Stub unless BOTH the runner ran with --live
    and COPILOT_JUDGE=live is set (belt and suspenders against accidental spend)."""
    if not live or os.environ.get("COPILOT_JUDGE", "stub") != "live":
        return {
            "verdict": True,
            "tier": "stub",
            "reason": "stub judge — deterministic rubrics are authoritative; set COPILOT_JUDGE=live with --live for the LLM tier",
        }

    import anthropic

    from app.config import get_settings

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    resp = client.messages.create(
        model=s.anthropic_model,
        max_tokens=512,
        system=(
            "You judge whether a clinical briefing is fully supported by its cited evidence. "
            "Answer ONLY via the judge_verdict tool. Any value, trend, or assertion not present "
            "in the evidence means consistent=false."
        ),
        tools=[JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "judge_verdict"},
        messages=[{
            "role": "user",
            "content": f"case={case_id}\n\nEVIDENCE:\n{evidence_text}\n\nBRIEFING:\n{briefing_text}",
        }],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "judge_verdict":
            return {
                "verdict": bool(block.input.get("consistent")),
                "tier": "live",
                "reason": str(block.input.get("reason", ""))[:500],
            }
    return {"verdict": False, "tier": "live", "reason": "judge returned no forced tool call"}
