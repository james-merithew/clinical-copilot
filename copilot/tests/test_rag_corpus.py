"""Hermetic tests for the guideline corpus loader + deterministic chunker.

Failure modes guarded:
- chunk_id instability would silently break every committed GuidelineRef
  citation target (chunk ids are shown to and stored for the physician);
- front-matter drift (missing doc_id/source_org/source_url) would strip
  attribution from guideline citations — the paraphrase policy requires it;
- unbounded chunks would blow the reranker/context budget.

Runs against the REAL committed corpus files — no keys, no DB, no network.
"""

from __future__ import annotations

import re

from app.rag.corpus import (
    DEFAULT_CORPUS_DIR,
    MAX_CHUNK_WORDS,
    chunk_document,
    load_corpus,
    parse_front_matter,
    slugify,
)

_CHUNK_ID = re.compile(r"^[a-z0-9-]+#[a-z0-9-]+:\d+$")


def test_corpus_files_present_with_required_front_matter():
    """BOUNDARY: all 9 committed corpus docs parse, each carrying the
    attribution front-matter (doc_id/title/source_org/source_url) that must
    travel with every guideline citation."""
    paths = sorted(DEFAULT_CORPUS_DIR.glob("*.md"))
    assert len(paths) == 9
    for p in paths:
        meta, body = parse_front_matter(p.read_text(encoding="utf-8"))
        for key in ("doc_id", "title", "source_org", "source_url"):
            assert meta.get(key), f"{p.name} missing front-matter key {key}"
        assert body.strip(), f"{p.name} has an empty body"


def test_chunker_is_deterministic_over_real_corpus():
    """INVARIANT (citation-target stability): loading the same corpus twice
    yields byte-identical chunk ids AND texts, in the same order."""
    a = load_corpus()
    b = load_corpus()
    assert [(c.chunk_id, c.text) for c in a] == [(c.chunk_id, c.text) for c in b]
    assert len(a) > 0


def test_chunk_ids_are_wellformed_unique_and_bounded():
    """INVARIANT: every chunk id matches `doc_id#slug:n`, ids never collide
    (they are primary citation keys), and no chunk exceeds the size budget."""
    chunks = load_corpus()
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))
    for c in chunks:
        assert _CHUNK_ID.match(c.chunk_id), c.chunk_id
        assert c.chunk_id.startswith(f"{c.doc_id}#")
        assert len(c.text.split()) <= MAX_CHUNK_WORDS
        assert c.section and c.source_org and c.source_url


def test_duplicate_headings_do_not_collide():
    """REGRESSION: two sections with the same heading (same slug) must still
    produce distinct chunk ids — the per-slug counter keeps counting."""
    meta = {"doc_id": "d", "title": "T", "source_org": "O", "source_url": "u"}
    body = "## Alpha\n\none two\n\n## Alpha\n\nthree four\n"
    ids = [c.chunk_id for c in chunk_document(meta, body)]
    assert ids == ["d#alpha:0", "d#alpha:1"]


def test_oversized_section_splits_into_sequential_chunks():
    """BOUNDARY: a section longer than the word budget splits into n>1 chunks
    with sequential suffixes, none exceeding the budget."""
    meta = {"doc_id": "d", "title": "T", "source_org": "O", "source_url": "u"}
    body = "## Big\n\n" + " ".join(f"w{i}" for i in range(MAX_CHUNK_WORDS * 2 + 10))
    chunks = chunk_document(meta, body)
    assert [c.chunk_id for c in chunks] == ["d#big:0", "d#big:1", "d#big:2"]
    assert all(len(c.text.split()) <= MAX_CHUNK_WORDS for c in chunks)


def test_slugify_normalizes():
    assert slugify("The eGFR bands (mL/min/1.73 m²)") == "the-egfr-bands-ml-min-1-73-m"
    assert slugify("  ") == "section"
