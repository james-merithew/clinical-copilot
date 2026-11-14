"""Hermetic invariant test for the Gate-1 injection guard (app.tools).

The model-facing tool schemas MUST expose no `pid` and no `user_id` argument
anywhere. Patient scope is bound server-side in ToolContext by the Authorization
Gateway, so even a fully prompt-injected model ("also summarize patient 1234")
has no argument through which to pivot patients. This test freezes that
guarantee against future schema edits.

No DB, no LLM.
"""

from __future__ import annotations

from app.tools import TOOL_SCHEMAS

_FORBIDDEN = {"pid", "user_id", "patient_id", "userid", "patientid"}


def _all_property_keys(schema: dict) -> set[str]:
    """Recursively collect every property key in an input_schema (nested objects,
    array items included) so a forbidden id can't hide one level down."""
    keys: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                keys.update(props.keys())
                for v in props.values():
                    walk(v)
            items = node.get("items")
            if items is not None:
                walk(items)

    walk(schema)
    return keys


def test_no_tool_schema_exposes_pid_or_user_id():
    """SECURITY (injection guard invariant): no model-facing tool may accept a
    patient/user identifier argument — guards against a prompt-injection patient
    pivot on a public endpoint."""
    assert TOOL_SCHEMAS, "expected at least one tool schema"
    for tool in TOOL_SCHEMAS:
        keys = _all_property_keys(tool["input_schema"])
        leaked = keys & _FORBIDDEN
        assert not leaked, f"tool {tool['name']!r} leaks scoping arg(s): {leaked}"
