"""Strict data contracts — the source of truth for every boundary.

These Pydantic models define tool I/O, verification, and the response payload.
The implementation follows the contracts, never the reverse (CLAUDE.md
invariant 5). The client payload is mirrored in Zod in W1_ARCHITECTURE.md §10.2.

Attribution primitives (SourceRef / Claim) are the backbone of the hard
source-attribution invariant: every value the agent surfaces carries the real
OpenEMR primary key it came from, so the Verifier can bind claims to evidence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, ClassVar, Literal, Union

from pydantic import BaseModel, BeforeValidator, Field, model_validator

# ---- Attribution primitives -------------------------------------------------

SourceTable = Literal[
    "form_encounter",
    "form_vitals",
    "prescriptions",
    "lists",
    "procedure_result",
    "billing",
    "patient_data",
]


class SourceRef(BaseModel):
    """A binding to one real OpenEMR row. `primary_key` MUST be a value the DB
    actually returned this turn — fabricated PKs fail the Verifier closed.

    Week 2: gains the `source_type` discriminator tag (defaulted, so Week 1
    constructors and payloads are unchanged) — see `AnySourceRef`."""

    source_type: Literal["patient_record"] = "patient_record"
    table: SourceTable
    primary_key: int = Field(..., description="Real DB PK of the source row")
    label: str = Field(..., description="Human label, e.g. 'ED visit 2026-06-14'")

    def key(self) -> tuple[str, int]:
        return (self.table, self.primary_key)


class BBox(BaseModel):
    """A page-relative bounding box in NORMALIZED 0–1 coordinates (resolution-
    independent — the UI renders it as percentage-positioned overlay divs).

    `origin` records coordinate authority: `text_layer` = deterministically
    located by pdfium text search (solid border, trusted); `vlm` = the model's
    own guess (dashed 'approximate' border, display hint only, never the
    verified region). W2_ARCHITECTURE.md §5.3 / D3."""

    page: int = Field(..., ge=1, description="1-based page number")
    x0: float = Field(..., ge=0.0, le=1.0)
    y0: float = Field(..., ge=0.0, le=1.0)
    x1: float = Field(..., ge=0.0, le=1.0)
    y1: float = Field(..., ge=0.0, le=1.0)
    origin: Literal["text_layer", "vlm"]


class GuidelineRef(BaseModel):
    """A binding to one chunk of the clinical-guideline corpus
    (`copilot/corpus/guidelines/`). Reference knowledge — NEVER a patient fact;
    the verifier's separation rule keeps gl-only claims out of the briefing."""

    source_type: Literal["guideline"] = "guideline"
    doc_id: str = Field(..., description="Corpus document id (YAML front-matter)")
    chunk_id: str = Field(..., description="Deterministic chunk id, `doc_id#slug:n`")
    section: str = Field(..., description="Heading the chunk came from")
    label: str = Field(..., description="Human label, e.g. 'ADA SoC — glycemic targets'")

    def key(self) -> tuple[str, str]:
        return ("guideline", self.chunk_id)


class DocumentRef(BaseModel):
    """A binding to a region of a stored document (`documents.id`), down to the
    extracted field and its verified bounding box — the click-to-source target
    for the bbox overlay viewer (W2_ARCHITECTURE.md §5.1/§5.3)."""

    source_type: Literal["document"] = "document"
    document_id: int = Field(..., description="Real `documents.id` of the stored file")
    page: int | None = Field(None, ge=1, description="1-based page the value was read from")
    field_id: str | None = Field(
        None, description="Extraction field id (provenance UNIQUE(document_id, field_id))"
    )
    bbox: BBox | None = None
    quote: str | None = Field(None, description="Verbatim text-layer quote containing the value")
    label: str = Field(..., description="Human label, e.g. 'Lab report p.1 — Glucose'")

    def key(self) -> tuple[str, str]:
        return ("documents", f"{self.document_id}:{self.field_id or ''}")


def _tag_untyped_as_patient_record(v: object) -> object:
    """Wire-compat shim: Week 1 payloads carry no `source_type` tag. A
    discriminated union rejects tag-less input, so a bare Week 1 dict is
    tagged `patient_record` before validation. Additive — tagged input is
    passed through untouched."""
    if isinstance(v, dict) and "source_type" not in v:
        return {**v, "source_type": "patient_record"}
    return v


# The Week 2 citation contract (design decision D1): one discriminated union
# instead of subclassing, which would hit the Pydantic subclass-serialization
# trap (a DocumentRef declared as a SourceRef field silently serializes as its
# base class, dropping page/bbox/quote). The client switches on `source_type`.
# Two Annotated layers so the wire-compat shim runs BEFORE tag extraction
# (metadata on one Annotated level does not guarantee that ordering).
_TaggedSourceRef = Annotated[
    Union[SourceRef, GuidelineRef, DocumentRef],
    Field(discriminator="source_type"),
]
AnySourceRef = Annotated[
    _TaggedSourceRef,
    BeforeValidator(_tag_untyped_as_patient_record),
]


class Claim(BaseModel):
    """A single factual statement plus the source rows that support it."""

    text: str
    source_refs: list[AnySourceRef] = Field(default_factory=list)


# ---- Requests ---------------------------------------------------------------


class ChatTurn(BaseModel):
    """One prior turn, for multi-turn context (no raw PHI persisted server-side;
    the client replays visible history)."""

    role: Literal["user", "assistant"]
    text: str


class ChatRequest(BaseModel):
    """A turn arrives as free text OR a chip tap. Both are first-class and
    resolve to the same agent + tools (W1_ARCHITECTURE.md §6). Exactly one of
    `message` / `chip_intent` is set per turn.

    NOTE: there is deliberately **no `user_id`** here. The requesting identity is
    NEVER taken from the client (it would be trivially spoofable on a public
    endpoint). It is pinned server-side at the Authorization Gateway (demo) or
    derived from an OpenEMR-validated token (production). The client may only
    choose the patient `pid`."""

    pid: int = Field(..., description="Single-patient scope")
    message: str | None = None  # free-text turn (core interface)
    chip_intent: "ChipIntent | None" = None  # suggested-reply accelerant
    chip_params: dict[str, int] = Field(default_factory=dict)
    history: list[ChatTurn] = Field(default_factory=list)
    correlation_id: str | None = None  # assigned at /chat if absent

    @model_validator(mode="after")
    def _exactly_one_input(self) -> "ChatRequest":
        has_msg = bool(self.message and self.message.strip())
        has_chip = self.chip_intent is not None
        if has_msg == has_chip:  # both or neither
            raise ValueError("provide exactly one of `message` or `chip_intent`")
        return self


class ToolRequest(BaseModel):
    """Every tool call is authorized and patient-scoped before it runs."""

    correlation_id: str
    user_id: int
    pid: int


# ---- Tool results (each record carries a SourceRef) -------------------------


# Each record exposes `.source` (a SourceRef) and `.evidence_text()` — a
# normalized string of its field values used by the Verifier for field-level
# grounding (a claim's salient tokens must appear in its cited evidence).
# `HANDLE_PREFIX` gives the opaque handle the model cites (e.g. "med:0").


class Encounter(BaseModel):
    HANDLE_PREFIX: ClassVar[str] = "enc"
    source: SourceRef
    date: str
    kind: Literal["office", "ed", "inpatient", "other"]
    reason: str | None = None
    provider: str | None = None

    def evidence_text(self) -> str:
        return f"{self.date} {self.kind} encounter reason={self.reason or ''} provider={self.provider or ''}"


class CareTransitionsResult(BaseModel):
    encounters: list[Encounter]
    truncated: bool = False


class Medication(BaseModel):
    HANDLE_PREFIX: ClassVar[str] = "med"
    source: SourceRef
    name: str
    dose: str | None = None
    status: str | None = None  # active / inactive as recorded
    started: str | None = None  # start_date (for "newly started" detection)
    # Divergence: same drug recorded in both `prescriptions` and `lists`.
    diverges: bool = False
    divergence_note: str | None = None

    def evidence_text(self) -> str:
        d = f" {self.dose}" if self.dose else ""
        s = f" {self.status}" if self.status else ""
        div = f" DIVERGENCE({self.divergence_note})" if self.diverges else ""
        return f"{self.name}{d}{s}{div}"


class MedicationsResult(BaseModel):
    medications: list[Medication]
    truncated: bool = False


class Problem(BaseModel):
    HANDLE_PREFIX: ClassVar[str] = "prob"
    source: SourceRef
    title: str
    status: str | None = None
    onset: str | None = None

    def evidence_text(self) -> str:
        return f"{self.title} status={self.status or ''} onset={self.onset or ''}"


class Allergy(BaseModel):
    HANDLE_PREFIX: ClassVar[str] = "alg"
    source: SourceRef
    substance: str
    reaction: str | None = None

    def evidence_text(self) -> str:
        return f"allergy to {self.substance} reaction={self.reaction or ''}"


class ProblemsResult(BaseModel):
    problems: list[Problem]
    allergies: list[Allergy]
    truncated: bool = False


class LabResult(BaseModel):
    HANDLE_PREFIX: ClassVar[str] = "lab"
    source: SourceRef
    name: str
    value: str | None = None
    units: str | None = None
    range: str | None = None
    abnormal: bool = False
    date: str | None = None

    def evidence_text(self) -> str:
        ab = " abnormal" if self.abnormal else ""
        return f"{self.name} {self.value or ''} {self.units or ''} range={self.range or ''}{ab} {self.date or ''}"


class LabsResult(BaseModel):
    labs: list[LabResult]
    truncated: bool = False


class Vital(BaseModel):
    HANDLE_PREFIX: ClassVar[str] = "vit"
    source: SourceRef
    date: str | None = None
    bps: float | None = None
    bpd: float | None = None
    pulse: float | None = None
    weight: float | None = None
    height: float | None = None
    bmi: float | None = None
    temperature: float | None = None
    spo2: float | None = None

    def evidence_text(self) -> str:
        parts = []
        if self.bps and self.bpd:
            parts.append(f"BP {self.bps:g}/{self.bpd:g}")
        if self.pulse:
            parts.append(f"HR {self.pulse:g}")
        if self.bmi:
            parts.append(f"BMI {self.bmi:g}")
        if self.temperature:
            parts.append(f"temp {self.temperature:g}")
        if self.spo2:
            parts.append(f"SpO2 {self.spo2:g}")
        return f"{self.date or ''} " + " ".join(parts)


class VitalsResult(BaseModel):
    vitals: list[Vital]
    truncated: bool = False


# ---- Guideline retrieval (Week 2, D4) ----------------------------------------


class EvidenceChunk(BaseModel):
    """One retrieved corpus chunk. Registered in the turn's EvidenceSet under
    `gl:` handles — same evidence-handle mechanics as every patient-record tool,
    so the verifier needs no special case for provenance."""

    HANDLE_PREFIX: ClassVar[str] = "gl"
    ref: GuidelineRef
    quote: str = Field(..., description="The chunk text surfaced to the model")
    score: float = Field(..., description="Post-fusion/rerank relevance score")
    source_org: str = Field(..., description="e.g. 'ADA' — attribution front-matter")
    source_url: str = Field(..., description="Primary-source URL for the physician")

    def evidence_text(self) -> str:
        return self.quote

    @property
    def source(self) -> GuidelineRef:
        """Alias so the agent's generic evidence registration (`rec.source`)
        works unchanged for guideline chunks — same path as patient records."""
        return self.ref


class GuidelineSearchResult(BaseModel):
    """Output of the `search_guidelines` tool."""

    chunks: list[EvidenceChunk] = Field(default_factory=list)
    provider: str = Field(..., description="Embedding provider actually used, e.g. 'cohere'|'tfidf'")
    reranker: str = Field(..., description="Reranker actually used, e.g. 'cohere'|'noop'")
    degraded: bool = False  # e.g. dense/rerank unavailable → BM25-only


# ---- Supervisor graph (Week 2, D5) --------------------------------------------

# Fixed node/route vocabularies — routing is enum-bound, never free text, so the
# handoff trail stays machine-inspectable (and log-safe: enum values only).
NodeName = Literal["supervisor", "intake_extractor", "evidence_retriever", "answerer", "critic"]
RouteName = Literal[
    "route_to_intake_extractor",
    "route_to_evidence_retriever",
    "finalize",
    "forced_finalize",  # budget/deadline exhaustion — the graph's graceful exit
    "worker_return",  # worker → supervisor transition carrying a WorkerResult
    "critique",  # answerer → critic: deterministic pre-verifier review
    "critic_passed",  # critic → answerer: nothing rejected or stripped
    "critic_rejected",  # critic → answerer: >=1 claim rejected / action stripped
]
WorkerName = Literal["intake_extractor", "evidence_retriever"]


class Handoff(BaseModel):
    """One graph transition — the inspectability contract (W2 §7.3). Emitted to
    three sinks: log_event (enum/ID fields ONLY — `reason`/`payload_summary`
    free text never reaches logs), LangSmith child runs, and
    `AgentResponse.handoffs` so the UI can render 'how this answer was
    assembled'."""

    handoff_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    seq: int = Field(..., ge=0, description="Order of this transition within the turn")
    from_node: NodeName
    to_node: NodeName
    route: RouteName
    reason: str = Field(..., description="Why the supervisor routed here (free text — UI/trace only)")
    payload_summary: str = Field(
        "", description="What was carried across (free text — UI/trace only, never logged)"
    )
    correlation_id: str
    ts: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 UTC timestamp",
    )
    latency_ms: float | None = None


class WorkerResult(BaseModel):
    """What a bounded worker sub-loop returned to the supervisor."""

    worker: WorkerName
    ok: bool
    summary: str = Field(..., description="Worker's summary for the supervisor (free text)")
    handles: list[str] = Field(
        default_factory=list, description="Evidence handles the worker added, e.g. ['gl:0']"
    )
    iterations: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


# ---- Critic (Week 2 extension — the graph's 4th node) -------------------------


class CriticFinding(BaseModel):
    """One deterministic critic hit. `text` is the offending claim/label — it
    rides the response (like quarantined_claims) and is NEVER logged; the log
    sink sees counts only."""

    kind: Literal["uncited_claim", "unsafe_action"]
    channel: Literal["briefing", "action_chip"]
    text: str
    disposition: Literal["rejected_to_quarantine", "stripped"]


class CriticReport(BaseModel):
    """What the critic node did this turn. Fully deterministic — no LLM pass;
    `model_review` is a defaulted seam for a future model-backed second opinion
    and is never set by the shipped critic."""

    reviewed: bool = False
    rejected_uncited: int = 0
    stripped_unsafe: int = 0
    findings: list[CriticFinding] = Field(default_factory=list)
    model_review: str | None = None


# ---- Verification -----------------------------------------------------------


class ConstraintSeverity(str, Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class ConstraintFlag(BaseModel):
    kind: Literal["allergy_interaction", "drug_drug", "threshold"]
    severity: ConstraintSeverity
    message: str
    source_refs: list[SourceRef]


class VerificationResult(BaseModel):
    verified_claims: list[Claim]
    quarantined_claims: list[Claim]
    constraint_flags: list[ConstraintFlag]
    # Week 2 (additive, defaulted): verified claims citing ONLY guideline
    # (`gl:`) handles — partitioned OUT of verified_claims by the verifier's
    # separation rule (W2 §6.3) so reference knowledge can never render as a
    # patient fact. Mixed claims (>=1 patient-record handle) stay in
    # verified_claims / the briefing.
    guideline_evidence: list[Claim] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """A turn 'passes' verification if nothing was quarantined."""
        return len(self.quarantined_claims) == 0


# ---- Steering ---------------------------------------------------------------


class ChipIntent(str, Enum):
    explain_care_transition = "explain_care_transition"
    lab_trend = "lab_trend"
    med_detail = "med_detail"
    care_gaps = "care_gaps"


class ActionChip(BaseModel):
    label: str  # e.g. "🔍 Why was he in the ED?"
    intent: ChipIntent  # enum-bound, not free text
    params: dict[str, int] = Field(default_factory=dict)


# ---- Response ---------------------------------------------------------------


class AgentResponse(BaseModel):
    correlation_id: str
    briefing: list[Claim]  # only VERIFIED claims (attributed + field-checked)
    # Week 2 (additive, defaulted — Week 1 consumers unchanged):
    # verified claims citing ONLY `gl:` handles. Rendered under a SEPARATE
    # header so reference knowledge is never confused with this patient's chart.
    guideline_evidence: list[Claim] = Field(default_factory=list)
    # The graph's transition trail ("how this answer was assembled").
    handoffs: list[Handoff] = Field(default_factory=list)
    constraint_flags: list[ConstraintFlag] = Field(default_factory=list)
    action_chips: list[ActionChip] = Field(default_factory=list)
    # Transparency: claims the Verifier held back. The UI renders these
    # explicitly as "unverified — needs chart review", never as fact. We ship
    # the texts (not just a count) so the client can show them honestly.
    quarantined_claims: list[Claim] = Field(default_factory=list)
    quarantined_count: int = 0
    # Week 2 extension (additive, defaulted): the deterministic critic node's
    # report — what it rejected to quarantine / stripped before the verifier.
    critic: CriticReport | None = None
    degraded: bool = False  # partial data? (graceful degradation)
    notes: str | None = None  # e.g. "Labs unavailable this turn"


class AuthzDenied(BaseModel):
    """Returned when the Authorization Gateway refuses a request. Clean refusal,
    nothing downstream runs."""

    correlation_id: str
    reason: str = "not_authorized"
    detail: str = "The requesting user is not authorized to view this patient."


# ---- Snapshot / dashboard (deterministic, no LLM) ---------------------------


class Demographics(BaseModel):
    source: SourceRef
    name: str
    age: int | None = None
    sex: str | None = None
    dob: str | None = None


class MetricPoint(BaseModel):
    value: str
    date: str | None = None


class Metric(BaseModel):
    """A key vital/lab value card: value + status color by default, trend on expand."""

    key: str
    label: str
    value: str
    unit: str | None = None
    status: Literal["normal", "elevated", "high", "low"] = "normal"
    status_label: str | None = None  # "High", "Stable", "Overweight", …
    group: Literal["vital", "lab"] = "lab"
    history: list[MetricPoint] = Field(default_factory=list)
    source: SourceRef | None = None


class SnapshotResponse(BaseModel):
    """At-a-glance patient snapshot powering the dashboard header + cards.
    Every item keeps its SourceRef so the UI shows the same citations as chat."""

    correlation_id: str
    demographics: Demographics | None = None
    metrics: list[Metric] = Field(default_factory=list)
    encounters: list[Encounter] = Field(default_factory=list)
    problems: list[Problem] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)
    allergies: list[Allergy] = Field(default_factory=list)
    labs_by_test: dict[str, list[LabResult]] = Field(default_factory=dict)
    constraint_flags: list[ConstraintFlag] = Field(default_factory=list)
    degraded: bool = False
    notes: str | None = None


class LabTrendResponse(BaseModel):
    """Deterministic lab-trend series powering the chart widget (no LLM) — a
    sibling of SnapshotResponse. Each point is a full `LabResult`, so every
    plotted value keeps its `procedure_result` SourceRef and the chart cites
    exactly like chat. Extracted-PDF Observations land in `procedure_result`
    too, so they flow into the trend with the same attribution."""

    correlation_id: str
    test: str | None = None  # echo of the ?test= filter (None = all tests)
    series: dict[str, list[LabResult]] = Field(default_factory=dict)  # name -> ASC by date
    truncated: bool = False
    notes: str | None = None  # e.g. "labs unavailable" (graceful degradation)


# ---- My Day (provider's schedule triage) ------------------------------------


class FlagSummary(BaseModel):
    critical: int = 0
    warning: int = 0


class DayAppointment(BaseModel):
    time: str
    pid: int
    demographics: Demographics | None = None
    reason: str | None = None
    flags: FlagSummary = Field(default_factory=FlagSummary)
    changed_recently: bool = False  # recent ED/inpatient transition


class DayResponse(BaseModel):
    correlation_id: str
    date: str
    provider_id: int
    count: int
    appointments: list[DayAppointment] = Field(default_factory=list)
    degraded: bool = False
    notes: str | None = None


ChatRequest.model_rebuild()
