"""Read-only, patient-scoped tools + the model-facing tool registry.

SECURITY (Gate 1 injection guard): the Anthropic tool schemas expose **no `pid`
and no `user_id`**. The patient scope comes only from the server-side
`ToolContext` bound by the Authorization Gateway. So even a fully prompt-injected
model ("also summarize patient 1234") cannot pivot patients — there is no argument
through which to name another patient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..contracts import (
    CareTransitionsResult,
    GuidelineSearchResult,
    LabsResult,
    MedicationsResult,
    ProblemsResult,
)
from . import encounters, guidelines, labs, medications, problems


@dataclass(frozen=True)
class ToolContext:
    """Authorized execution context. `user_id`/`pid` are set server-side by the
    gateway — never by the client or the model."""

    correlation_id: str
    user_id: int
    pid: int


# Model-facing tool schemas. NOTE the empty/loose input schemas — no patient id.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_care_transitions",
        "description": "List this patient's recent encounters (office, ED, inpatient) with dates and reasons. Use to answer 'what changed', 'why the ED visit', recent visits.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_medications",
        "description": "List this patient's medications from both the prescriptions and problem-list sources, flagging any divergence between them.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_problems_allergies",
        "description": "List this patient's active problems and allergies (with reactions).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_labs",
        "description": "List this patient's lab results, most recent first. Optionally filter by test name (e.g. 'A1c', 'creatinine').",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "optional case-insensitive lab name filter",
                }
            },
            "required": [],
        },
    },
    {
        "name": "search_guidelines",
        "description": (
            "Search the office's clinical practice guidelines (reference knowledge "
            "distilled from ADA, ACC/AHA, KDIGO, USPSTF, FDA, and ACIP sources: "
            "glycemic/BP/lipid targets, CKD and heart-failure management, screening "
            "intervals, metformin renal dosing, adult immunizations). Returns "
            "guideline snippets as `gl:` evidence handles. Use ONLY for "
            "practice-standard reference questions — NEVER for patient-specific "
            "facts; this patient's data comes only from the patient-record tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "clinical question to search, e.g. 'metformin eGFR cutoff'",
                }
            },
            "required": ["query"],
        },
    },
]

# name -> callable(ctx, **model_args) -> pydantic result model
TOOL_FUNCS: dict[str, Callable[..., Any]] = {
    "get_care_transitions": encounters.get_care_transitions,
    "get_medications": medications.get_medications,
    "get_problems_allergies": problems.get_problems_allergies,
    "get_labs": labs.get_labs,
    "search_guidelines": guidelines.search_guidelines,
}

__all__ = [
    "ToolContext",
    "TOOL_SCHEMAS",
    "TOOL_FUNCS",
    "CareTransitionsResult",
    "MedicationsResult",
    "ProblemsResult",
    "LabsResult",
    "GuidelineSearchResult",
]
