"""Hermetic tests for the `search_guidelines` tool registration and the
agent's `gl:` evidence-handle registration path.

Failure modes guarded:
- the tool missing from TOOL_SCHEMAS/TOOL_FUNCS (model could never call it);
- a schema that leaks patient scope (pid must NEVER be a tool argument);
- guideline chunks registering under the wrong prefix or the wrong ref type,
  which would break the verifier's separation rule silently.

The index is injected (monkeypatched) — no settings, keys, or network.
"""

from __future__ import annotations

from app import agent
from app.rag.corpus import load_corpus
from app.rag.index import HybridIndex
from app.rag.providers import LocalTfidfEmbedding, NoopReranker
from app.tools import TOOL_FUNCS, TOOL_SCHEMAS, ToolContext, guidelines


def _schema(name: str) -> dict:
    return next(t for t in TOOL_SCHEMAS if t["name"] == name)


def test_search_guidelines_is_registered_with_guardrail_description():
    """CONTRACT: registered in both registries; the description carries the
    'NEVER for patient-specific facts' guardrail; query is the only, required
    argument — no pid/user_id surface (injection guard preserved)."""
    schema = _schema("search_guidelines")
    assert "NEVER for patient-specific facts" in schema["description"]
    assert set(schema["input_schema"]["properties"]) == {"query"}
    assert schema["input_schema"]["required"] == ["query"]
    assert TOOL_FUNCS["search_guidelines"] is guidelines.search_guidelines


def test_agent_registers_chunks_as_gl_handles(monkeypatch):
    """INVARIANT (handle discipline): search_guidelines results register in the
    turn EvidenceSet as gl:N handles whose source_ref is a GuidelineRef — the
    exact shape the verifier's separation rule partitions on."""
    idx = HybridIndex(load_corpus(), LocalTfidfEmbedding(), NoopReranker(), top_k=3)
    monkeypatch.setattr(guidelines, "get_index", lambda: idx)

    turn = agent._Turn()
    ctx = ToolContext(correlation_id="cid-test", user_id=1, pid=101)
    out = agent._run_tool("search_guidelines", {"query": "metformin eGFR cutoff"}, ctx, turn)

    assert out, "expected retrieval hits over the real corpus"
    assert [o["handle"] for o in out] == [f"gl:{i}" for i in range(len(out))]
    for o in out:
        item = turn.evidence.get(o["handle"])
        assert item is not None
        assert item.source_ref.source_type == "guideline"
        assert item.source_ref.chunk_id.startswith(item.source_ref.doc_id + "#")
        assert o["data"] == item.text  # the model sees the chunk quote verbatim
    assert len(turn.gathered["guidelines"]) == len(out)


def test_gl_and_patient_handles_share_one_evidence_set(monkeypatch):
    """REGRESSION: gl: handles coexist with patient handles in the same turn
    EvidenceSet without counter collisions (separate prefix namespaces)."""
    from app.contracts import SourceRef

    idx = HybridIndex(load_corpus(), LocalTfidfEmbedding(), NoopReranker(), top_k=2)
    monkeypatch.setattr(guidelines, "get_index", lambda: idx)

    turn = agent._Turn()
    turn.evidence.add("med", SourceRef(table="prescriptions", primary_key=1, label="x"), "Metformin 1000 mg")
    ctx = ToolContext(correlation_id="cid-test", user_id=1, pid=101)
    agent._run_tool("search_guidelines", {"query": "metformin renal dosing"}, ctx, turn)

    assert turn.evidence.get("med:0") is not None
    assert turn.evidence.get("gl:0") is not None
    assert turn.evidence.get("gl:0").source_ref.source_type == "guideline"
    assert turn.evidence.get("med:0").source_ref.source_type == "patient_record"
