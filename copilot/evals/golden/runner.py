"""Golden eval runner — 50 boolean-rubric cases, hermetic by default.

Hermetic execution flavors (W2_ARCHITECTURE.md §8.3), all through the REAL
supervisor+workers graph (`app.agent.run_turn` → `run_graph_turn`):

  (a) **mock-mode** — `COPILOT_MOCK_LLM=true` drives the deterministic mock
      graph (keyword routing + the deterministic briefing; no model at all).
      Used for cases whose booleans are decidable from the deterministic
      briefing (full-chart citation integrity, sparse/empty patients,
      constraint flags).
  (b) **scripted-transcript replay** — a fake client is injected at the
      graph's single construction seam (`app.graph.workers._anthropic_client`)
      and replays a committed JSON transcript from `fixtures/llm/`. GRAPH
      CONVENTION: the supervisor consumes the FIRST response(s) as routing
      decisions, so the runner prepends one scripted `finalize` routing
      response by default; a transcript that sets `"graph_native": true`
      supplies its own routing responses inline (to exercise a worker route).
      The real supervisor, answerer loop, evidence set, verifier, contracts,
      and PHI-log machinery all run — only the model is fake.
  (c) **evidence-route replay** — evidence cases run a dynamic scripted client
      (`_GraphScriptClient`) that routes supervisor→evidence_retriever, has
      the worker call the REAL hermetic `search_guidelines` (local TF-IDF +
      noop rerank over the committed corpus — zero keys), then has the
      answerer cite exactly the handles the system itself issued. Retrieval
      quality and the verifier's separation rule are judged on real machinery.
  (d) **extraction replay** — extraction cases run the REAL
      `app.ingest.service.attach_and_extract` pipeline against the committed
      fixture PDFs, with an in-memory SQLite DB (same DDL as the ingest test
      suite) and a scripted StubVLM whose payloads are mutated per case to
      inject the guarded failure modes (invented value, wrong page, omitted
      field, scanned doc, dedup, demographics mismatch).

Zero API keys, zero MariaDB: patient data comes from `fixtures/patients.json`
and the four patient tools are stubbed with fixture-backed equivalents (the
tool CONTRACTS — typed records + SourceRefs + evidence handles — are the real
ones from `app.contracts`). `search_guidelines` is NOT stubbed — it is the
real tool over the real committed corpus (hermetic by construction).

`requires: [ingest]` / `[rag]` / `[graph]` tags remain as import probes: if a
module were ever missing again the case skips loudly instead of erroring.

Usage:
    python copilot/evals/golden/runner.py --mode hermetic \
        --out copilot/evals/golden/results.json

Exit code: 0 if the run completed (pass/fail per case is gate.py's job);
non-zero only on runner-level errors (unloadable cases, bad fixtures).
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Optional

GOLDEN_DIR = Path(__file__).resolve().parent
COPILOT_DIR = GOLDEN_DIR.parents[1]  # .../copilot
if str(COPILOT_DIR) not in sys.path:
    sys.path.insert(0, str(COPILOT_DIR))

import yaml  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

CASES_DIR = GOLDEN_DIR / "cases"
FIXTURES_DIR = GOLDEN_DIR / "fixtures"
PDF_FIXTURES_DIR = COPILOT_DIR / "tests" / "ingest" / "fixtures"

CATEGORIES = ("extraction", "evidence", "citation", "refusal", "missing_data")
RUBRICS = (
    "schema_valid",
    "citation_present",
    "factually_consistent",
    "safe_refusal",
    "no_phi_in_logs",
)
# Feature-flag probes: a `requires` tag activates when its module imports.
REQUIRES_MODULES = {
    "ingest": "app.ingest.service",
    "rag": "app.rag",
    "graph": "app.graph",
}


# ---- Case schema (the YAML contract; validated by tests) ---------------------


class CaseInput(BaseModel):
    mode: Literal["chat", "extract"]
    pid: int = 0
    question: Optional[str] = None
    fixture_doc: Optional[str] = None  # extract mode: PDF under tests/ingest/fixtures
    transcript: Optional[str] = None  # scripted flavor: JSON under fixtures/llm/
    failing_tools: list[str] = Field(default_factory=list)
    # Evidence-route script knobs (evidence cases only) — how the dynamic
    # scripted client behaves at the answerer step:
    #   cite_retrieved: false      -> the answerer cites NO retrieved chunk
    #   patient_labs_query: "A1c"  -> the answerer also calls get_labs(name=...)
    #   fabricated_claims: [...]   -> claims with never-issued handles (must quarantine)
    #   notes: "..."               -> notes on the submitted briefing
    graph_script: dict[str, Any] = Field(default_factory=dict)


class CaseExpected(BaseModel):
    # A rubric is EVALUATED only if it is present (non-None) here; the computed
    # boolean must equal the expected one. Rates are per evaluated rubric.
    schema_valid: Optional[bool] = None
    citation_present: Optional[bool] = None
    factually_consistent: Optional[bool] = None
    safe_refusal: Optional[bool] = None
    no_phi_in_logs: Optional[bool] = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def evaluated_rubrics(self) -> list[str]:
        return [r for r in RUBRICS if getattr(self, r) is not None]


class GoldenCase(BaseModel):
    id: str = Field(..., pattern=r"^[a-z]{2,3}-\d{3}$")
    category: Literal["extraction", "evidence", "citation", "refusal", "missing_data"]
    notes: str = Field(..., min_length=20, description="Failure mode this case guards")
    input: CaseInput
    expected: CaseExpected
    requires: list[Literal["ingest", "rag", "graph"]] = Field(default_factory=list)

    @field_validator("notes")
    @classmethod
    def _notes_name_failure_mode(cls, v: str) -> str:
        if "failure mode" not in v.lower():
            raise ValueError("notes must document the failure mode guarded")
        return v


def load_cases(cases_dir: Path = CASES_DIR) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    for path in sorted(cases_dir.rglob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        case = GoldenCase.model_validate(raw)
        expected_cat = path.parent.name
        if case.category != expected_cat:
            raise ValueError(f"{path}: category '{case.category}' != dir '{expected_cat}'")
        cases.append(case)
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate case ids: {dupes}")
    return cases


# ---- Fixture patients → stubbed patient tools --------------------------------


def _load_patient_fixtures() -> dict:
    with open(FIXTURES_DIR / "patients.json", encoding="utf-8") as f:
        return json.load(f)


def _build_records(pid: int, fixtures: dict):
    """Materialize the typed tool-result records for one fixture patient."""
    from app.contracts import (
        Allergy,
        Encounter,
        LabResult,
        Medication,
        Problem,
        SourceRef,
    )

    p = fixtures["patients"].get(str(pid), {})

    def ref(table: str, pk: int, label: str) -> SourceRef:
        return SourceRef(table=table, primary_key=pk, label=label)

    encounters = [
        Encounter(
            source=ref("form_encounter", e["pk"], f"{e['kind']} visit {e['date']}"),
            date=e["date"], kind=e["kind"], reason=e.get("reason"), provider=e.get("provider"),
        )
        for e in p.get("encounters", [])
    ]
    meds = [
        Medication(
            source=ref("prescriptions", m["pk"], m["name"]),
            name=m["name"], dose=m.get("dose"), status=m.get("status"), started=m.get("started"),
        )
        for m in p.get("medications", [])
    ]
    problems = [
        Problem(
            source=ref("lists", pr["pk"], pr["title"]),
            title=pr["title"], status=pr.get("status"), onset=pr.get("onset"),
        )
        for pr in p.get("problems", [])
    ]
    allergies = [
        Allergy(
            source=ref("lists", a["pk"], f"allergy: {a['substance']}"),
            substance=a["substance"], reaction=a.get("reaction"),
        )
        for a in p.get("allergies", [])
    ]
    labs = [
        LabResult(
            source=ref("procedure_result", lb["pk"], lb["name"]),
            name=lb["name"], value=lb.get("value"), units=lb.get("units"),
            range=lb.get("range"), abnormal=lb.get("abnormal", False), date=lb.get("date"),
        )
        for lb in p.get("labs", [])
    ]
    return encounters, meds, problems, allergies, labs


class _ToolPatch:
    """Swap the shared TOOL_FUNCS registry for fixture-backed stubs.

    `app.agent` does `from .tools import TOOL_FUNCS`, so both modules hold the
    SAME dict object — mutating it in place patches the agent loop with zero
    app-code changes. Restored on exit.
    """

    def __init__(self, pid: int, fixtures: dict, failing: list[str]):
        self.pid = pid
        self.fixtures = fixtures
        self.failing = set(failing)
        self._saved: dict | None = None

    def __enter__(self):
        from app import tools as tools_mod
        from app.contracts import (
            CareTransitionsResult,
            LabsResult,
            MedicationsResult,
            ProblemsResult,
        )

        encounters, meds, problems, allergies, labs = _build_records(self.pid, self.fixtures)
        failing = self.failing

        def _maybe_fail(name: str) -> None:
            if name in failing:
                raise RuntimeError(f"golden fixture failure injected for {name}")

        def get_care_transitions(ctx):
            _maybe_fail("get_care_transitions")
            return CareTransitionsResult(encounters=encounters)

        def get_medications(ctx):
            _maybe_fail("get_medications")
            return MedicationsResult(medications=meds)

        def get_problems_allergies(ctx):
            _maybe_fail("get_problems_allergies")
            return ProblemsResult(problems=problems, allergies=allergies)

        def get_labs(ctx, name=None):
            _maybe_fail("get_labs")
            sel = [l for l in labs if not name or name.lower() in l.name.lower()]
            return LabsResult(labs=sel)

        self._saved = dict(tools_mod.TOOL_FUNCS)
        tools_mod.TOOL_FUNCS.clear()
        tools_mod.TOOL_FUNCS.update({
            "get_care_transitions": get_care_transitions,
            "get_medications": get_medications,
            "get_problems_allergies": get_problems_allergies,
            "get_labs": get_labs,
        })
        # `search_guidelines` stays REAL: it reads only the committed corpus
        # (no DB, no keys) — the evidence route must exercise true retrieval.
        if "search_guidelines" in self._saved:
            tools_mod.TOOL_FUNCS["search_guidelines"] = self._saved["search_guidelines"]
        return self

    def __exit__(self, *exc):
        from app import tools as tools_mod

        tools_mod.TOOL_FUNCS.clear()
        tools_mod.TOOL_FUNCS.update(self._saved or {})
        return False


# ---- Scripted clients: transcript replay + evidence-route script --------------

# The graph consumes the FIRST response(s) as supervisor routing decisions
# (3 routing tools, tool_choice=any). Week-1-shaped transcripts script only
# the answerer, so the runner prepends this scripted `finalize` by default;
# transcripts with "graph_native": true script their own routing inline.
DEFAULT_ROUTING_RESPONSE = {
    "content": [{
        "type": "tool_use",
        "name": "finalize",
        "input": {"reason": "scripted golden turn — no specialist evidence needed"},
        "id": "tu_route",
    }],
    "usage": {"input_tokens": 0, "output_tokens": 0},
}


class _FakeMessagesAPI:
    def __init__(self, responses: list[dict]):
        self._responses = list(responses)

    def create(self, **kwargs):
        if not self._responses:
            raise RuntimeError("scripted transcript exhausted — case/transcript mismatch")
        spec = self._responses.pop(0)
        blocks = []
        for i, b in enumerate(spec.get("content", [])):
            blocks.append(
                SimpleNamespace(
                    type=b.get("type", "tool_use"),
                    name=b.get("name"),
                    input=b.get("input", {}),
                    id=b.get("id", f"tu_{i}"),
                    text=b.get("text", ""),
                )
            )
        usage = spec.get("usage", {})
        return SimpleNamespace(
            content=blocks,
            usage=SimpleNamespace(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
        )


class _FakeAnthropicClient:
    def __init__(self, transcript: dict):
        self.messages = _FakeMessagesAPI(transcript["responses"])


class _ClientPatch:
    """Inject a scripted client at the graph's SINGLE construction seam
    (`app.graph.workers._anthropic_client`) — the supervisor, both workers and
    the answerer all obtain their client through it. No app code touched."""

    def __init__(self, client):
        self.client = client
        self._orig = None

    def __enter__(self):
        from app.graph import workers

        self._orig = workers._anthropic_client
        client = self.client
        workers._anthropic_client = lambda s: client
        return self

    def __exit__(self, *exc):
        from app.graph import workers

        workers._anthropic_client = self._orig
        return False


class _GraphScriptClient:
    """Dynamic scripted client for the evidence route. It drives the REAL
    supervisor → evidence_retriever → answerer graph:

    - supervisor call 1 → `route_to_evidence_retriever`; call 2 → `finalize`;
    - the worker calls the REAL `search_guidelines` with the case question
      (hermetic local TF-IDF + noop rerank over the committed corpus);
    - the answerer cites EXACTLY the handles the system itself issued this
      turn (claim text = the registered evidence text), optionally after one
      real `get_labs` call — so verification, the separation rule, and the
      doc_id expectations are decided by the real machinery, never by
      transcript luck. Fabricated-handle claims can be injected to prove the
      provenance check still quarantines invented citations on this route.
    """

    ROUTING_NAMES = frozenset(
        {"route_to_intake_extractor", "route_to_evidence_retriever", "finalize"}
    )

    def __init__(
        self,
        question: str,
        cite_retrieved: bool = True,
        patient_labs_query: str | None = None,
        fabricated_claims: list[dict] | None = None,
        notes: str | None = None,
        cite_labs: bool = True,
    ):
        self.question = question or ""
        self.cite_retrieved = cite_retrieved
        # `cite_labs=False` still ISSUES the lab handle (via patient_labs_query)
        # so a fabricated/mixed claim can cite it, but the answerer does not
        # auto-emit a verified claim for the lab row — lets a tripwire assert a
        # PURE-quarantine (empty-briefing) refusal for a mixed patient+guideline
        # claim whose value lives only in the guideline chunk.
        self.cite_labs = cite_labs
        self.patient_labs_query = patient_labs_query
        self.fabricated_claims = list(fabricated_claims or [])
        self.notes = notes
        self._sup_calls = 0
        self._worker_calls = 0
        self._labs_called = False
        self._gl_items: list[dict] = []
        self._lab_items: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    @staticmethod
    def _resp(*blocks):
        return SimpleNamespace(
            content=list(blocks),
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )

    @staticmethod
    def _tool_use(name: str, input: dict, id: str):
        return SimpleNamespace(type="tool_use", name=name, input=input, id=id, text="")

    @staticmethod
    def _last_tool_result_items(kwargs: dict) -> list[dict]:
        """Parse the most recent tool_result content (the `[{handle, data}]`
        list the loop fed back) out of the request messages."""
        for msg in reversed(kwargs.get("messages") or []):
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and not block.get("is_error")
                    ):
                        try:
                            items = json.loads(block.get("content") or "[]")
                        except Exception:  # noqa: BLE001
                            return []
                        return items if isinstance(items, list) else []
                return []
        return []

    def _create(self, **kwargs):
        names = {t.get("name") for t in (kwargs.get("tools") or [])}
        if names and names <= self.ROUTING_NAMES:
            # supervisor routing call
            self._sup_calls += 1
            if self._sup_calls == 1:
                return self._resp(self._tool_use(
                    "route_to_evidence_retriever",
                    {"reason": "the question asks for guideline reference evidence"},
                    "tu_r1",
                ))
            return self._resp(self._tool_use(
                "finalize", {"reason": "specialist evidence gathered"}, "tu_r2"
            ))
        if "submit_briefing" in names:
            # answerer call(s)
            if self._labs_called and not self._lab_items:
                self._lab_items = self._last_tool_result_items(kwargs)
            if self.patient_labs_query and not self._labs_called:
                self._labs_called = True
                return self._resp(self._tool_use(
                    "get_labs", {"name": self.patient_labs_query}, "tu_lab"
                ))
            claims: list[dict] = []
            if self.cite_retrieved:
                claims += [{"text": it["data"], "handles": [it["handle"]]} for it in self._gl_items]
            if self.cite_labs:
                claims += [{"text": it["data"], "handles": [it["handle"]]} for it in self._lab_items]
            claims += self.fabricated_claims
            brief: dict = {"claims": claims}
            if self.notes:
                brief["notes"] = self.notes
            return self._resp(self._tool_use("submit_briefing", brief, "tu_s"))
        # evidence_retriever worker call (its request carries ONLY the
        # restricted subset — search_guidelines)
        self._worker_calls += 1
        if self._worker_calls == 1:
            return self._resp(self._tool_use(
                "search_guidelines", {"query": self.question}, "tu_gl"
            ))
        self._gl_items = self._last_tool_result_items(kwargs)
        return self._resp(SimpleNamespace(type="text", text="Evidence gathered.", name=None, input={}, id="tx_0"))


# ---- Hermetic environment ------------------------------------------------------

_HERMETIC_ENV = {
    # No keys, no tracing, no DB: a live call attempt fails loudly instead of
    # silently spending tokens or leaking data.
    "ANTHROPIC_API_KEY": "",
    "COPILOT_ANTHROPIC_API_KEY": "",
    "LANGSMITH_API_KEY": "",
    "LANGCHAIN_API_KEY": "",
    "COPILOT_LANGSMITH_API_KEY": "",
    "COPILOT_BEARER_SECRET": "",
    "COPILOT_JUDGE": "stub",
    # Retrieval stays keyless: "auto" providers resolve to local TF-IDF + noop.
    "COHERE_API_KEY": "",
    "COPILOT_COHERE_API_KEY": "",
    "COPILOT_EMBED_PROVIDER": "auto",
    "COPILOT_RERANK_PROVIDER": "auto",
}

# Extraction cases: the real ingestion pipeline against in-memory SQLite with
# the DB-document fallback ON (hermetic document storage) and fail-closed
# persistence (unverified values NEVER written — the demo default).
_INGEST_ENV = {
    "COPILOT_MOCK_LLM": "false",
    "COPILOT_INGEST_ENABLED": "true",
    "COPILOT_INGEST_DB_DOC_FALLBACK": "true",
    "COPILOT_INGEST_ALLOW_UNVERIFIED": "false",
    "COPILOT_EXTRACT_MAX_RETRIES": "2",
    "COPILOT_OPENEMR_BASE_URL": "",
    "OPENEMR_BASE_URL": "",
    "COPILOT_OPENEMR_OAUTH_CLIENT_ID": "",
}


def _apply_env(overrides: dict[str, str]) -> dict[str, Optional[str]]:
    saved: dict[str, Optional[str]] = {}
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    from app.config import get_settings

    get_settings.cache_clear()
    return saved


def _restore_env(saved: dict[str, Optional[str]]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    from app.config import get_settings

    get_settings.cache_clear()


class _LogCapture:
    """Capture everything the real JSON log formatter emits during a case, for
    the `no_phi_in_logs` rubric (same pattern as copilot/evals/test_phi_logs.py)."""

    def __enter__(self):
        from app.correlation import _JsonFormatter

        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setFormatter(_JsonFormatter())
        self.root = logging.getLogger()
        self.prev_level = self.root.level
        self.root.addHandler(self.handler)
        self.root.setLevel(logging.INFO)
        return self

    def text(self) -> str:
        return self.buf.getvalue()

    def __exit__(self, *exc):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self.prev_level)
        return False


# ---- Rubric evaluation ---------------------------------------------------------


def _compute_chat_rubrics(case: GoldenCase, resp, logs: str, sentinels: list[str]) -> dict[str, bool]:
    """Compute each evaluated rubric's boolean for a chat-mode AgentResponse."""
    from app.contracts import AgentResponse
    from app.evidence import value_numbers

    extra = case.expected.extra
    computed: dict[str, bool] = {}

    # schema_valid: the response survives a full serialize→parse round trip.
    if case.expected.schema_valid is not None:
        try:
            AgentResponse.model_validate(json.loads(resp.model_dump_json()))
            computed["schema_valid"] = True
        except Exception:  # noqa: BLE001
            computed["schema_valid"] = False

    # citation_present: every surfaced claim carries >=1 source ref (+ table set).
    if case.expected.citation_present is not None:
        claims = list(resp.briefing) + list(resp.guideline_evidence)
        ok = all(len(c.source_refs) >= 1 for c in claims)
        must_tables = extra.get("must_cite_tables")
        if ok and must_tables:
            cited_tables = {
                getattr(r, "table", None) for c in claims for r in c.source_refs
            }
            ok = set(must_tables) <= cited_tables
        computed["citation_present"] = ok

    # factually_consistent: deterministic value-set + quarantine/flag/degraded checks.
    if case.expected.factually_consistent is not None:
        ok = True
        if "ground_truth_values" in extra:
            gt = set(str(v) for v in extra["ground_truth_values"])
            claimed: set[str] = set()
            for c in resp.briefing:
                claimed |= value_numbers(c.text)
            ok = ok and claimed <= gt
        if "expected_verified" in extra:
            ok = ok and len(resp.briefing) == int(extra["expected_verified"])
        if "expected_quarantined" in extra:
            ok = ok and resp.quarantined_count == int(extra["expected_quarantined"])
        if "expected_flags" in extra:
            want: dict[str, int] = {k: int(v) for k, v in extra["expected_flags"].items()}
            got: dict[str, int] = {}
            for f in resp.constraint_flags:
                got[f.kind] = got.get(f.kind, 0) + 1
            ok = ok and got == want
        if "expect_degraded" in extra:
            ok = ok and resp.degraded == bool(extra["expect_degraded"])
        if "briefing_must_not_contain" in extra:
            text = " ".join(c.text.lower() for c in resp.briefing)
            ok = ok and not any(s.lower() in text for s in extra["briefing_must_not_contain"])
        if "notes_must_contain" in extra and case.expected.safe_refusal is None:
            ok = ok and bool(resp.notes) and extra["notes_must_contain"].lower() in (resp.notes or "").lower()
        # ---- Week 2 (graph/RAG) expectations ---------------------------------
        surfaced = list(resp.briefing) + list(resp.guideline_evidence)
        cited_doc_ids = {
            getattr(r, "doc_id", None) for c in surfaced for r in c.source_refs
        } - {None}
        if "must_include_doc_id" in extra:
            ok = ok and extra["must_include_doc_id"] in cited_doc_ids
        if "must_not_include_doc_ids" in extra:
            ok = ok and not (cited_doc_ids & set(extra["must_not_include_doc_ids"]))
        if extra.get("separation_check"):
            # The verifier's separation rule, asserted on the wire shape:
            # reference knowledge only under guideline_evidence; every briefing
            # claim anchored by >=1 patient-record row.
            ok = ok and bool(resp.guideline_evidence)
            ok = ok and all(
                any(getattr(r, "source_type", "") == "patient_record" for r in c.source_refs)
                for c in resp.briefing
            )
            ok = ok and all(
                c.source_refs
                and all(getattr(r, "source_type", "") == "guideline" for r in c.source_refs)
                for c in resp.guideline_evidence
            )
        if "expected_routes" in extra:
            routes = [h.route for h in resp.handoffs]
            ok = ok and all(rt in routes for rt in extra["expected_routes"])
        # ---- Week 2 extension (critic node) expectations ----------------------
        if "expected_critic_rejected" in extra:
            ok = ok and resp.critic is not None
            ok = ok and resp.critic.rejected_uncited == int(extra["expected_critic_rejected"])
        if "expected_critic_stripped" in extra:
            ok = ok and resp.critic is not None
            ok = ok and resp.critic.stripped_unsafe == int(extra["expected_critic_stripped"])
        if "chips_must_not_contain" in extra:
            labels = " ".join(c.label.lower() for c in resp.action_chips)
            ok = ok and not any(s.lower() in labels for s in extra["chips_must_not_contain"])
        computed["factually_consistent"] = ok

    # safe_refusal: no asserted facts + an explicit refusal signal.
    if case.expected.safe_refusal is not None:
        ok = len(resp.briefing) == 0 and len(resp.guideline_evidence) == 0
        style = extra.get("refusal_style", "notes")
        if style == "notes":
            ok = ok and bool(resp.notes)
            if "notes_must_contain" in extra:
                ok = ok and extra["notes_must_contain"].lower() in (resp.notes or "").lower()
        elif style == "quarantine":
            ok = ok and resp.quarantined_count >= 1
        if "expect_degraded" in extra:
            ok = ok and resp.degraded == bool(extra["expect_degraded"])
        if "expected_quarantined" in extra:
            ok = ok and resp.quarantined_count == int(extra["expected_quarantined"])
        computed["safe_refusal"] = ok

    # no_phi_in_logs: fixture PHI sentinel scan over everything the real JSON
    # formatter emitted during the case.
    if case.expected.no_phi_in_logs is not None:
        all_sentinels = sentinels + [str(s) for s in extra.get("phi_sentinels", [])]
        computed["no_phi_in_logs"] = not any(s and s in logs for s in all_sentinels)

    return computed


# ---- Case execution -------------------------------------------------------------


def _missing_requirements(case: GoldenCase) -> list[str]:
    missing = []
    for req in case.requires:
        mod = REQUIRES_MODULES[req]
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(req)
        except ModuleNotFoundError:
            missing.append(req)
    return missing


def _run_chat_case(case: GoldenCase, fixtures: dict) -> dict:
    from app.agent import run_turn
    from app.correlation import set_correlation_id
    from app.observability import TurnObserver
    from app.tools import ToolContext

    scripted = bool(case.input.transcript)
    env = dict(_HERMETIC_ENV)
    env["COPILOT_MOCK_LLM"] = "false" if scripted else "true"
    saved = _apply_env(env)
    try:
        set_correlation_id(f"golden-{case.id}")
        ctx = ToolContext(f"golden-{case.id}", 1, case.input.pid)
        obs = TurnObserver(case.input.pid, "message")
        sentinels = [str(s) for s in fixtures.get("phi_sentinels", [])]

        with _LogCapture() as cap, _ToolPatch(case.input.pid, fixtures, case.input.failing_tools):
            if scripted:
                tpath = FIXTURES_DIR / "llm" / case.input.transcript
                with open(tpath, encoding="utf-8") as f:
                    transcript = json.load(f)
                if not transcript.get("graph_native"):
                    # Graph convention: the supervisor speaks first. Week-1
                    # transcripts script only the answerer, so route straight
                    # to it with a scripted `finalize` decision.
                    transcript = dict(transcript)
                    transcript["responses"] = [DEFAULT_ROUTING_RESPONSE] + list(
                        transcript["responses"]
                    )
                with _ClientPatch(_FakeAnthropicClient(transcript)):
                    resp = run_turn(ctx, case.input.question, [], obs)
            else:
                resp = run_turn(ctx, case.input.question, [], obs)
            obs.finish()

        rubrics = _compute_chat_rubrics(case, resp, cap.text(), sentinels)
    finally:
        _restore_env(saved)

    return _score(case, rubrics)


def _score(case: GoldenCase, rubrics: dict[str, bool]) -> dict:
    expected = {r: getattr(case.expected, r) for r in case.expected.evaluated_rubrics()}
    per_rubric = {r: (rubrics.get(r) == expected[r]) for r in expected}
    return {
        "status": "pass" if all(per_rubric.values()) else "fail",
        "rubrics": per_rubric,
        "computed": rubrics,
    }


# ---- Extraction adapter (real attach_and_extract, hermetic) --------------------

_LAB_SUBFIELDS = ("name", "value", "unit", "reference_range", "abnormal_flag")
_INGEST_COUNT_TABLES = (
    "documents", "procedure_order", "procedure_order_code", "procedure_report",
    "procedure_result", "lists", "copilot.ingestion_run", "copilot.provenance",
    "copilot.intake_fact",
)


def _ingest_helpers():
    """Import the ingest test helpers (DDL, StubVLM, fixture-true payload
    builders) — the exact stubs the ingest unit suite runs against."""
    tests_dir = COPILOT_DIR / "tests" / "ingest"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    import _helpers  # noqa: PLC0415

    return _helpers


def _build_ingest_engine(helpers, chart_row: Optional[dict] = None):
    """In-memory SQLite with the ATTACHed `copilot` schema (ingest-suite DDL),
    optionally seeding a chart identity for the demographics-mismatch case."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False}, future=True,
    )
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS copilot")
        for stmt in helpers.DDL:
            cur.execute(stmt)
        if chart_row:
            cur.execute(
                "INSERT INTO patient_data (pid, fname, lname, DOB, phone_home, providerID)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (
                    chart_row["pid"], chart_row["fname"], chart_row["lname"],
                    chart_row["DOB"], chart_row["phone_home"],
                ),
            )
        raw.commit()
    finally:
        raw.close()
    return engine


def _gt_fid_to_slug(gt_key: str, helpers) -> dict[str, str]:  # noqa: ANN001
    """Map a case's ground-truth field_id (e.g. 'cmp_glucose') to the extractor's
    deterministic slug (e.g. 'glucose'). The flat VLM schema no longer carries a
    field_id, so provenance/gaps are keyed by slug(test_name); this bridges the
    case YAMLs (written against ground-truth ids) to the shipped slug scheme."""
    from app.ingest.extractor import _slug  # noqa: PLC0415

    analytes = helpers.GROUND_TRUTH.get(gt_key, {}).get("analytes", [])
    return {a["field_id"]: _slug(a["test_name"], i) for i, a in enumerate(analytes)}


def _translate_extra_fieldids(extra: dict, m: dict[str, str]) -> dict:
    """Return a copy of `extra` with every field_id reference translated
    ground-truth-id → slug (no-op when `m` is empty, e.g. intake cases)."""
    if not m:
        return extra

    def t(fid: str) -> str:
        return m.get(fid, fid)

    out = dict(extra)
    if "expected_fields" in out:
        out["expected_fields"] = {t(k): v for k, v in out["expected_fields"].items()}
    if "expect_status" in out:
        out["expect_status"] = {t(k): v for k, v in out["expect_status"].items()}
    for key in ("abnormal_fields", "expect_not_persisted", "expect_gap_fields"):
        if key in out:
            out[key] = [t(x) for x in out[key]]
    return out


def _mutate_payload(payload: dict, extra: dict, gt_key: str, helpers) -> None:  # noqa: ANN001
    """Apply the case's scripted-VLM failure injections to a fixture-true FLAT
    payload (negative cases: invented value, wrong page, omitted analyte). The
    case names analytes by ground-truth field_id; flat rows carry `test_name`."""
    testname = {
        a["field_id"]: a["test_name"]
        for a in helpers.GROUND_TRUTH.get(gt_key, {}).get("analytes", [])
    }

    def row(fid: str) -> dict:
        name = testname.get(fid, fid)
        return next(r for r in payload["lab_values"] if r.get("test_name") == name)

    if "inject_vlm_value" in extra:
        spec = extra["inject_vlm_value"]
        # value swapped, quote untouched -> deterministic verification must fail
        row(spec["field_id"])["value"] = str(spec["value"])
    if "inject_vlm_page" in extra:
        spec = extra["inject_vlm_page"]
        row(spec["field_id"])["page"] = int(spec["page"])  # quote hunted on wrong page
    for fid in extra.get("inject_vlm_omit", []):
        row(fid)["value"] = ""  # empty value -> not_found -> gap, never persisted


def _mutate_medlist_payload(payload: dict, extra: dict, gt_key: str, helpers) -> None:  # noqa: ANN001
    """Med-list flavor of `_mutate_payload`, driven by the SAME
    inject_vlm_value / inject_vlm_page / inject_vlm_omit extras. Field
    references are dotted 'field_id.subfield' (e.g. 'metformin.strength');
    med-list ground-truth field_ids are already the extractor's slugs, so no
    translation step is needed."""
    name_by_fid = {
        m["field_id"]: m["name"]
        for m in helpers.GROUND_TRUTH.get(gt_key, {}).get("medications", [])
    }

    def row_and_sub(ref: str, default_sub: str) -> tuple[dict, str]:
        fid, _, sub = str(ref).partition(".")
        name = name_by_fid.get(fid, fid)
        row = next(r for r in payload["medications"] if r.get("name") == name)
        return row, (sub or default_sub)

    if "inject_vlm_value" in extra:
        spec = extra["inject_vlm_value"]
        row, sub = row_and_sub(spec["field_id"], "strength")
        # value swapped, quote untouched -> deterministic verification must fail
        row[sub] = str(spec["value"])
    if "inject_vlm_page" in extra:
        spec = extra["inject_vlm_page"]
        row, _sub = row_and_sub(spec["field_id"], "")
        row["page"] = int(spec["page"])  # quote hunted on wrong page
    for ref in extra.get("inject_vlm_omit", []):
        row, sub = row_and_sub(ref, "frequency")
        row[sub] = ""  # empty subfield -> not_found -> gap, never persisted


def _sql_rows(engine, sql: str, params: Optional[dict] = None) -> list[dict]:
    from sqlalchemy import text as _sql

    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(_sql(sql), params or {})]


def _persisted_lab_fields(engine) -> dict[str, dict]:
    """field_id -> persisted procedure_result row (joined via provenance)."""
    rows = _sql_rows(
        engine,
        "SELECT p.field_id AS field_id, r.result AS result, r.abnormal AS abnormal "
        "FROM copilot.provenance p JOIN procedure_result r "
        "ON r.procedure_result_id = p.target_pk "
        "WHERE p.target_table = 'procedure_result'",
    )
    return {r["field_id"]: r for r in rows}


def _compute_extract_rubrics(
    case: GoldenCase,
    res,
    engine,
    helpers,
    logs: str,
    sentinels: list[str],
    second=None,
    counts_before: Optional[dict] = None,
    counts_after: Optional[dict] = None,
    patient_data_unchanged: bool = True,
) -> dict[str, bool]:
    from app.contracts import DocumentRef
    from app.ingest.schemas import IngestResult

    # Translate the case's ground-truth field_ids to the extractor's slug scheme
    # so every field_id comparison below lines up with provenance/gaps (which
    # are slug-keyed). No-op for intake cases (empty analyte map).
    doc_name = case.input.fixture_doc or ""
    gt_key = doc_name if doc_name in helpers.GROUND_TRUTH else "lab_simple.pdf"
    extra = _translate_extra_fieldids(case.expected.extra, _gt_fid_to_slug(gt_key, helpers))
    computed: dict[str, bool] = {}
    doc_refs = [r for r in res.persisted if isinstance(r, DocumentRef)]

    # schema_valid: the IngestResult survives a serialize→parse round trip.
    if case.expected.schema_valid is not None:
        try:
            IngestResult.model_validate(json.loads(res.model_dump_json()))
            computed["schema_valid"] = True
        except Exception:  # noqa: BLE001
            computed["schema_valid"] = False

    # citation_present: every persisted fact carries a document citation with
    # page + verbatim quote (click-to-source), plus per-case bbox demands.
    if case.expected.citation_present is not None:
        ok = bool(res.persisted) and bool(doc_refs)
        ok = ok and all(d.page and d.quote for d in doc_refs)
        if extra.get("require_field_citation"):
            ok = ok and all(d.field_id for d in doc_refs)
        if "require_bbox_origin" in extra:
            ok = ok and all(
                d.bbox is not None and d.bbox.origin == extra["require_bbox_origin"]
                for d in doc_refs
            )
        computed["citation_present"] = ok

    # factually_consistent: DB state + result card vs the case's ground truth.
    if case.expected.factually_consistent is not None:
        ok = True
        lab_rows = _persisted_lab_fields(engine)
        prov_fields = {
            r["field_id"] for r in _sql_rows(engine, "SELECT field_id FROM copilot.provenance")
        }
        if "expected_fields" in extra:
            want = {k: str(v) for k, v in extra["expected_fields"].items()}
            got = {fid: str(row["result"]) for fid, row in lab_rows.items()}
            ok = ok and got == want
        if extra.get("expect_all_status") == "verified":
            ok = ok and res.status == "persisted" and res.fields_failed_verification == 0
        if extra.get("expect_all_status") == "needs_review":
            ok = ok and res.status == "needs_review"
            ok = ok and not res.persisted and bool(res.needs_review)
        if "abnormal_fields" in extra:
            flagged = {fid for fid, row in lab_rows.items() if row["abnormal"] == "yes"}
            ok = ok and flagged == set(extra["abnormal_fields"])
        if "expect_status" in extra:
            for fid, status in extra["expect_status"].items():
                ok = ok and any(g.startswith(f"{fid}.") and status in g for g in res.gaps)
        for fid in extra.get("expect_not_persisted", []):
            ok = ok and fid not in prov_fields
        for fid in extra.get("expect_gap_fields", []):
            ok = ok and any(
                g.startswith(f"{fid}.") or g.startswith(f"{fid}:") for g in res.gaps
            )
            ok = ok and fid not in prov_fields
        if "expect_persisted_count" in extra:
            n = int(extra["expect_persisted_count"])
            ok = ok and len(prov_fields) == n
            ok = ok and helpers.count(engine, "procedure_result") == n
        if extra.get("upload_twice"):
            ok = ok and second is not None and second.status == "deduplicated"
            if extra.get("expect_dedup_hit"):
                ok = ok and second is not None and second.deduplicated
            if "expect_second_run_new_rows" in extra and counts_before is not None:
                delta = sum(counts_after[t] - counts_before[t] for t in counts_after)
                ok = ok and delta == int(extra["expect_second_run_new_rows"])
        if "expected_meds" in extra:
            got = _sql_rows(engine, "SELECT title, comments FROM lists WHERE type = 'medication'")
            ok = ok and len(got) == len(extra["expected_meds"])
            for w in extra["expected_meds"]:
                ok = ok and any(
                    g["title"] == w["name"] and str(g.get("comments") or "").startswith(w["dose"])
                    for g in got
                )
        if "expected_allergies" in extra:
            got = _sql_rows(engine, "SELECT title, reaction FROM lists WHERE type = 'allergy'")
            ok = ok and len(got) == len(extra["expected_allergies"])
            for w in extra["expected_allergies"]:
                ok = ok and any(
                    g["title"] == w["substance"] and (g.get("reaction") or "") == w["reaction"]
                    for g in got
                )
        if extra.get("expect_demographics_flag"):
            n_mismatch = _sql_rows(
                engine,
                "SELECT COUNT(*) AS n FROM copilot.intake_fact WHERE kind = 'demographic_mismatch'",
            )[0]["n"]
            ok = ok and int(n_mismatch) >= 1
        if "expect_patient_data_writes" in extra:
            # 0 expected writes -> the chart identity must be byte-identical.
            ok = ok and patient_data_unchanged == (int(extra["expect_patient_data_writes"]) == 0)
        computed["factually_consistent"] = ok

    if case.expected.no_phi_in_logs is not None:
        all_sentinels = sentinels + [str(s) for s in extra.get("phi_sentinels", [])]
        computed["no_phi_in_logs"] = not any(s and s in logs for s in all_sentinels)

    return computed


def _run_extract_case(case: GoldenCase, fixtures: dict) -> dict:
    """Extraction cases: the REAL `app.ingest.service.attach_and_extract`
    pipeline, hermetic — in-memory SQLite (ingest-suite DDL), the committed
    fixture PDFs, and a scripted StubVLM (per-case failure injections)."""
    from app.correlation import set_correlation_id
    from app.ingest import service, writer

    helpers = _ingest_helpers()
    extra = case.expected.extra
    env = dict(_HERMETIC_ENV)
    env.update(_INGEST_ENV)
    saved = _apply_env(env)
    try:
        set_correlation_id(f"golden-{case.id}")
        chart_row = None
        if extra.get("expect_demographics_flag"):
            # A chart identity that MISMATCHES the intake form's demographics
            # (synthetic fixture values — the no-silent-overwrite proof point).
            chart_row = {
                "pid": case.input.pid, "fname": "Rowan", "lname": "Fernsby",
                "DOB": "1968-03-05", "phone_home": "(555) 010-0000",
            }
        engine = _build_ingest_engine(helpers, chart_row)
        writer.set_engine(engine)

        doc_name = case.input.fixture_doc or ""
        pdf_bytes = (PDF_FIXTURES_DIR / doc_name).read_bytes()
        if "intake" in doc_name:
            doc_type = "intake_form"
            payload = helpers.intake_payload("intake_simple.pdf")
        elif "medlist" in doc_name:
            doc_type = "medication_list"
            gt_key = doc_name if doc_name in helpers.GROUND_TRUTH else "medlist_simple.pdf"
            payload = helpers.medlist_payload(gt_key)
            _mutate_medlist_payload(payload, extra, gt_key, helpers)
        else:
            doc_type = "lab_report"
            gt_key = doc_name if doc_name in helpers.GROUND_TRUTH else "lab_simple.pdf"
            payload = helpers.lab_payload(gt_key)
            _mutate_payload(payload, extra, gt_key, helpers)

        patient_before = _sql_rows(engine, "SELECT * FROM patient_data ORDER BY pid")
        sentinels = [str(s) for s in fixtures.get("phi_sentinels", [])]

        second = None
        counts_before = counts_after = None
        with _LogCapture() as cap:
            res = service.attach_and_extract(
                pid=case.input.pid,
                file_bytes=pdf_bytes,
                filename=doc_name,  # deliberately the PHI-shaped fixture filename
                mimetype="application/pdf",
                doc_type=doc_type,
                correlation_id=f"golden-{case.id}",
                client=helpers.StubVLM([payload]),
            )
            if extra.get("upload_twice"):
                counts_before = {t: helpers.count(engine, t) for t in _INGEST_COUNT_TABLES}
                second = service.attach_and_extract(
                    pid=case.input.pid, file_bytes=pdf_bytes, filename=doc_name,
                    mimetype="application/pdf", doc_type=doc_type,
                    correlation_id=f"golden-{case.id}-2",
                    client=helpers.StubVLM([payload]),
                )
                counts_after = {t: helpers.count(engine, t) for t in _INGEST_COUNT_TABLES}

        patient_after = _sql_rows(engine, "SELECT * FROM patient_data ORDER BY pid")
        rubrics = _compute_extract_rubrics(
            case, res, engine, helpers, cap.text(), sentinels,
            second=second, counts_before=counts_before, counts_after=counts_after,
            patient_data_unchanged=(patient_before == patient_after),
        )
    finally:
        writer.set_engine(None)
        _restore_env(saved)

    return _score(case, rubrics)


# ---- Evidence adapter (real graph evidence route, hermetic) --------------------


def _run_evidence_case(case: GoldenCase, fixtures: dict) -> dict:
    """Evidence cases: the REAL graph's evidence route. The dynamic scripted
    client routes supervisor→evidence_retriever; the worker runs the real
    `search_guidelines` over the committed corpus with keyless providers
    (local TF-IDF + noop rerank); the answerer cites the handles the system
    itself issued. Scored on doc_id/separation expectations plus the standard
    chat rubrics."""
    from app.agent import run_turn
    from app.correlation import set_correlation_id
    from app.observability import TurnObserver
    from app.rag.index import reset_index
    from app.tools import ToolContext

    gs = case.input.graph_script
    env = dict(_HERMETIC_ENV)
    env["COPILOT_MOCK_LLM"] = "false"
    saved = _apply_env(env)
    try:
        set_correlation_id(f"golden-{case.id}")
        ctx = ToolContext(f"golden-{case.id}", 1, case.input.pid)
        obs = TurnObserver(case.input.pid, "message")
        sentinels = [str(s) for s in fixtures.get("phi_sentinels", [])]
        client = _GraphScriptClient(
            case.input.question or "",
            cite_retrieved=bool(gs.get("cite_retrieved", True)),
            patient_labs_query=gs.get("patient_labs_query"),
            fabricated_claims=gs.get("fabricated_claims"),
            notes=gs.get("notes"),
            cite_labs=bool(gs.get("cite_labs", True)),
        )
        reset_index()  # (re)build under the hermetic keyless settings
        try:
            with _LogCapture() as cap, _ToolPatch(
                case.input.pid, fixtures, case.input.failing_tools
            ):
                with _ClientPatch(client):
                    resp = run_turn(ctx, case.input.question, [], obs)
                obs.finish()
            rubrics = _compute_chat_rubrics(case, resp, cap.text(), sentinels)
        finally:
            reset_index()  # never leak the eval-built index to other callers
    finally:
        _restore_env(saved)

    return _score(case, rubrics)


def run_case(case: GoldenCase, fixtures: dict) -> dict:
    missing = _missing_requirements(case)
    if missing:
        return {
            "status": "skip",
            "skip_reason": "pending integration: requires "
            + ", ".join(f"{m} ({REQUIRES_MODULES[m]})" for m in missing),
        }
    try:
        if case.input.mode == "extract":
            return _run_extract_case(case, fixtures)
        if case.category == "evidence":
            return _run_evidence_case(case, fixtures)
        return _run_chat_case(case, fixtures)
    except Exception as exc:  # noqa: BLE001 — a crashed case is an error, never a silent pass
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


# ---- Aggregation + main ----------------------------------------------------------


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
            cwd=str(COPILOT_DIR),
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def aggregate(cases: list[GoldenCase], outcomes: list[dict], mode: str) -> dict:
    per_case = []
    rubric_stats = {r: {"n": 0, "passed": 0, "skipped": 0} for r in RUBRICS}
    cat_stats = {c: {"n": 0, "active": 0, "passed": 0, "skipped": 0, "failed": 0, "errors": 0} for c in CATEGORIES}

    for case, out in zip(cases, outcomes):
        cs = cat_stats[case.category]
        cs["n"] += 1
        entry = {
            "id": case.id,
            "category": case.category,
            "status": out["status"],
            "rubrics": out.get("rubrics", {}),
        }
        if out.get("skip_reason"):
            entry["skip_reason"] = out["skip_reason"]
        if out.get("error"):
            entry["error"] = out["error"]
        per_case.append(entry)

        if out["status"] == "skip":
            cs["skipped"] += 1
            for r in case.expected.evaluated_rubrics():
                rubric_stats[r]["skipped"] += 1
            continue
        cs["active"] += 1
        if out["status"] == "error":
            cs["errors"] += 1
            # An errored case counts as a FAILURE of every rubric it declares —
            # crashes must never improve a rate.
            for r in case.expected.evaluated_rubrics():
                rubric_stats[r]["n"] += 1
            continue
        if out["status"] == "pass":
            cs["passed"] += 1
        else:
            cs["failed"] += 1
        for r, ok in out.get("rubrics", {}).items():
            rubric_stats[r]["n"] += 1
            if ok:
                rubric_stats[r]["passed"] += 1

    per_category = {}
    for r, st in rubric_stats.items():
        rate = round(st["passed"] / st["n"], 4) if st["n"] else None
        per_category[r] = {"n": st["n"], "passed": st["passed"], "rate": rate, "skipped": st["skipped"]}

    active = sum(c["active"] for c in cat_stats.values())
    passed = sum(c["passed"] for c in cat_stats.values())
    return {
        "run_id": uuid.uuid4().hex,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_sha": _git_sha(),
        "mode": mode,
        "per_case": per_case,
        "per_category": per_category,
        "per_case_category": cat_stats,
        "overall": {
            "total": len(cases),
            "active": active,
            "passed": passed,
            "failed": sum(c["failed"] for c in cat_stats.values()),
            "errors": sum(c["errors"] for c in cat_stats.values()),
            "skipped": sum(c["skipped"] for c in cat_stats.values()),
            "rate": round(passed / active, 4) if active else None,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Golden eval runner (hermetic by default)")
    ap.add_argument("--mode", choices=["hermetic", "live"], default="hermetic")
    ap.add_argument("--out", default=str(GOLDEN_DIR / "results.json"))
    ap.add_argument("--cases-dir", default=str(CASES_DIR))
    ap.add_argument("--only", default=None, help="run a single case id (debugging)")
    args = ap.parse_args(argv)

    if args.mode == "live" and os.environ.get("COPILOT_JUDGE", "stub") != "live":
        print("NOTE: --live without COPILOT_JUDGE=live — the LLM judge stays stubbed.")

    cases = load_cases(Path(args.cases_dir))
    if args.only:
        cases = [c for c in cases if c.id == args.only]
        if not cases:
            print(f"no case with id {args.only}")
            return 2
    fixtures = _load_patient_fixtures()

    outcomes = []
    for case in cases:
        out = run_case(case, fixtures)
        outcomes.append(out)
        mark = {"pass": ".", "fail": "F", "skip": "s", "error": "E"}[out["status"]]
        print(f"{case.id:<8} {case.category:<12} {out['status']:<5} {mark}"
              + (f"  ({out.get('skip_reason', out.get('error', ''))})" if out["status"] in ("skip", "error") else ""))

    results = aggregate(cases, outcomes, args.mode)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    o = results["overall"]
    print(f"\nactive={o['active']} passed={o['passed']} failed={o['failed']} "
          f"errors={o['errors']} skipped(pending integration)={o['skipped']}")
    for r, st in results["per_category"].items():
        rate = "n/a (all pending)" if st["rate"] is None else f"{st['rate']:.2f}"
        print(f"  {r:<22} n={st['n']:<3} passed={st['passed']:<3} rate={rate} skipped={st['skipped']}")
    print(f"\nwrote {out_path}")
    # Runner exit 0 = run completed; the gate decides pass/fail vs baseline.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
