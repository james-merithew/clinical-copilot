"""Deterministic clinical query expansion (`app.rag.rewrite`) — unit tests.

Expansion is a RECALL fix for the retrieval legs only: it appends full-form
synonym phrases for whole-token clinical abbreviations to the END of the
query, byte-deterministically. It never rewrites or deletes the user's words.

Failure modes guarded:
- substring matching ("birth control" tripping the `bp` entry, "shtn"
  tripping `htn`) would inject unrelated clinical concepts into retrieval;
- nondeterminism (dict-order or set-order dependence) would make retrieval —
  and therefore which chunk gets CITED — irreproducible across runs;
- mutation of the original query would break the reranker's anchor on user
  intent (the reranker must see the user's own words, verbatim);
- duplicate phrases from overlapping keys (a1c + hba1c) skewing BM25 term
  frequencies.
"""

from __future__ import annotations

from app.rag.rewrite import SYNONYMS, expand_query


def test_appends_known_abbreviation_synonyms_after_original():
    out = expand_query("HTN goal")
    assert out.startswith("HTN goal")  # original tokens never deleted/replaced
    assert "hypertension" in out
    assert "blood pressure" in out


def test_whole_token_matches_only():
    # "birth control" contains the letters b-p nowhere as a token — and even a
    # word *containing* an abbreviation ("shtn") must not trip the entry.
    assert expand_query("birth control") == "birth control"
    assert expand_query("shtn stage") == "shtn stage"
    assert expand_query("champion routine") == "champion routine"  # no 'mi'


def test_noop_identity_when_nothing_matches():
    q = "metformin dosing when kidney function declines"
    assert expand_query(q) == q


def test_expansions_append_in_sorted_key_order():
    # Keys sort "ckd" < "htn": CKD's phrase must precede HTN's phrases,
    # regardless of the order the abbreviations appear in the query.
    out = expand_query("HTN and CKD")
    assert out == "HTN and CKD chronic kidney disease hypertension blood pressure"


def test_overlapping_keys_deduplicate_phrases():
    # a1c and hba1c expand to the same phrases — appended exactly once.
    out = expand_query("A1c and HbA1c")
    assert out == "A1c and HbA1c hemoglobin a1c glycated hemoglobin"


def test_byte_deterministic_across_calls():
    q = "CKD in DM with HTN and low eGFR"
    assert expand_query(q) == expand_query(q)


def test_case_insensitive_matching():
    assert expand_query("htn goal") == expand_query("HTN goal").replace("HTN", "htn")


def test_synonyms_cover_the_required_clinical_abbreviations():
    # The 9-doc corpus vocabulary the ticket pins (shrinking one of these
    # entries is allowed; dropping the key is not).
    for key in ("a1c", "hba1c", "htn", "bp", "dm", "t2dm", "ckd", "egfr", "hf", "ascvd", "mi"):
        assert key in SYNONYMS and SYNONYMS[key]
