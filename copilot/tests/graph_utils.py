"""Plain (non-fixture) hermetic helpers for the graph test suite: stub patient
tools, scripted FakeAnthropic client, and Anthropic content-block builders.
Kept out of conftest.py so tests can import them unambiguously."""

from __future__ import annotations

from types import SimpleNamespace

from app.contracts import (
    CareTransitionsResult,
    Encounter,
    LabResult,
    LabsResult,
    Medication,
    MedicationsResult,
    Problem,
    ProblemsResult,
    SourceRef,
)

# ---- Stub patient tools (deterministic, no DB) --------------------------------


def _src(table: str, pk: int, label: str) -> SourceRef:
    return SourceRef(table=table, primary_key=pk, label=label)


def stub_care_transitions(ctx):
    return CareTransitionsResult(encounters=[
        Encounter(source=_src("form_encounter", 7, "ED visit"), date="2026-07-01",
                  kind="ed", reason="chest pain", provider="Dr. A"),
    ])


def stub_medications(ctx):
    return MedicationsResult(medications=[
        Medication(source=_src("prescriptions", 3, "lisinopril"), name="lisinopril",
                   dose="10 mg", status="active"),
    ])


def stub_problems_allergies(ctx):
    return ProblemsResult(
        problems=[Problem(source=_src("lists", 5, "hypertension"),
                          title="hypertension", status="active")],
        allergies=[],
    )


def stub_labs(ctx, name=None):
    # Distinctive values (8.7, 142) so PHI tests can assert they never leak
    # into worker summaries or handoff logs. abnormal=False + no range keeps
    # the constraint engine quiet for the byte-compat regression test.
    return LabsResult(labs=[
        LabResult(source=_src("procedure_result", 11, "A1c"), name="Hemoglobin A1c",
                  value="8.7", units="%", abnormal=False, date="2026-07-01"),
        LabResult(source=_src("procedure_result", 12, "Sodium"), name="Sodium",
                  value="142", units="mmol/L", abnormal=False, date="2026-07-01"),
    ])


# ---- Scripted FakeAnthropic client --------------------------------------------


def tool_use(name: str, input: dict | None = None, id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=input or {}, id=id)


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def fake_resp(*blocks):
    return SimpleNamespace(
        content=list(blocks),
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def submit_briefing_resp(claims=None, notes=None):
    payload = {"claims": claims or []}
    if notes is not None:
        payload["notes"] = notes
    return fake_resp(tool_use("submit_briefing", payload))


class FakeAnthropic:
    """Returns scripted responses in order; records every create() kwargs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAnthropic script exhausted — unexpected extra LLM call")
        return self._responses.pop(0)

    @property
    def consumed(self) -> int:
        return len(self.calls)
