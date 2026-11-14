"""Full-path integration test — upload → extract → persist → chat → grounded answer.

This is the one test the Week-2 rubric's "integration tests with fixtures and stubs"
line asks for: a SINGLE hermetic flow that stitches the two halves the golden runner
otherwise exercises separately.

    1. ingest a fixture lab PDF through the REAL `attach_and_extract` pipeline, with a
       scripted `StubVLM` (no Anthropic/VLM API) → persists the OpenEMR lab chain
       (procedure_order → … → procedure_result) into an in-memory SQLite engine;
    2. run a REAL chat turn through the REAL supervisor→worker→answerer graph, with a
       scripted LLM client (no Anthropic API), whose REAL `get_labs` tool reads that
       SAME engine and whose verified answer cites the just-ingested row's real
       `procedure_result` primary key.

No live API keys, no MariaDB — it runs in CI under plain `pytest`. The point it proves
that neither half proves alone: a document a physician uploads becomes a
source-attributed fact the co-pilot will cite, end to end, with the model untrusted at
both the extract and answer steps.

The runner internals (`_build_ingest_engine`, `_GraphScriptClient`, `_ClientPatch`,
the hermetic env) are reused verbatim so this test and the golden gate share one
hermetic substrate rather than drifting a second copy.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

_COPILOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = _COPILOT / "evals" / "golden"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"golden_{name}", GOLDEN_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"golden_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


runner = _load("runner")


def _ingest_helpers():
    """The ingest-suite helpers (StubVLM, DDL, canned payloads)."""
    sys.path.insert(0, str(_COPILOT / "tests" / "ingest"))
    import _helpers  # noqa: PLC0415

    return _helpers


@pytest.fixture
def hermetic_env():
    env = dict(runner._HERMETIC_ENV)
    env.update(runner._INGEST_ENV)
    saved = runner._apply_env(env)
    yield
    runner._restore_env(saved)


def test_uploaded_lab_becomes_a_cited_fact_end_to_end(hermetic_env):
    helpers = _ingest_helpers()
    from app import db as db_mod
    from app.agent import run_turn
    from app.correlation import set_correlation_id
    from app.ingest import service, writer
    from app.observability import TurnObserver
    from app.rag.index import reset_index
    from app.tools import ToolContext

    pid = 101  # Marcus Delgado — seeded in the ingest DDL's patient_data
    engine = runner._build_ingest_engine(helpers)

    # Point BOTH the ingest writer AND the read-only tool layer at ONE engine, so
    # what ingestion persists is exactly what the chat tool later reads. (Set the
    # db module's cached engine directly — bypassing get_engine() also skips its
    # MariaDB-only SET SESSION max_statement_time hook, which SQLite rejects.)
    writer.set_engine(engine)
    saved_engine = db_mod._engine
    db_mod._engine = engine
    try:
        # ---- 1. INGEST: fixture PDF → StubVLM → verified persist -------------------
        set_correlation_id("e2e-ingest")
        payload = helpers.lab_payload("lab_simple.pdf")
        res = service.attach_and_extract(
            pid=pid,
            file_bytes=helpers.LAB_SIMPLE_PDF,
            filename="lab_simple.pdf",
            mimetype="application/pdf",
            doc_type="lab_report",
            correlation_id="e2e-ingest",
            client=helpers.StubVLM([payload]),
        )
        assert res.status == "persisted", res
        assert res.fields_extracted >= 1 and res.deduplicated is False, res

        # The ingested Glucose row is now the authoritative lab chain. Grab its PK
        # straight from the DB so the assertion binds to a REAL primary key, not a
        # value the model could have hallucinated.
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT procedure_result_id AS pk, result, units FROM procedure_result "
                    "WHERE result_text = 'Glucose'"
                )
            ).fetchall()
        assert len(rows) == 1, "exactly one ingested Glucose result expected"
        glucose_pk = int(rows[0].pk)
        assert str(rows[0].result) == "142"

        # ---- 2. CHAT: real graph + real get_labs over the ingested engine ----------
        set_correlation_id("e2e-chat")
        question = "What is the patient's most recent glucose result?"
        ctx = ToolContext("e2e-chat", 1, pid)
        obs = TurnObserver(pid, "message")
        # Scripted client: routes through the graph and issues a REAL get_labs
        # (patient_labs_query) whose result the answerer cites. No _ToolPatch here —
        # get_labs runs for real against the ingested SQLite.
        client = runner._GraphScriptClient(
            question,
            cite_retrieved=False,   # this turn's grounded claim is the patient lab, not guidelines
            patient_labs_query="Glucose",
            cite_labs=True,
        )
        reset_index()
        try:
            with runner._ClientPatch(client):
                resp = run_turn(ctx, question, [], obs)
            obs.finish()
        finally:
            reset_index()
    finally:
        writer.set_engine(None)
        db_mod._engine = saved_engine

    # ---- 3. ASSERT: the answer cites the ingested row's real PK, with its value ----
    assert resp.briefing, "expected a grounded patient-record claim, got an empty briefing"
    cited = [
        (r.table, int(r.primary_key))
        for c in resp.briefing
        for r in c.source_refs
        if getattr(r, "table", None) == "procedure_result"
    ]
    assert ("procedure_result", glucose_pk) in cited, (
        f"answer must cite the ingested Glucose row pk={glucose_pk}; cited={cited}"
    )
    # the cited claim actually carries the ingested value — a real fact, not a shape
    assert any("142" in c.text for c in resp.briefing), (
        f"cited claim should surface the ingested value 142; briefing={[c.text for c in resp.briefing]}"
    )
    # nothing about this well-formed, real-sourced claim should be quarantined
    assert resp.quarantined_count == 0, resp.quarantined_count
