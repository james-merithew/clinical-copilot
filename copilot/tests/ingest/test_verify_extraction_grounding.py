"""Regression tests for the deterministic value-in-quote check (D3).

Failure mode guarded (confirmed adversarial finding, CRITICAL): `_value_in_quote`
used raw SUBSTRING containment, so a VLM digit-drop/truncation whose digits are a
substring of the genuine row text verified at confidence 0.95 —
  '14'  is a substring of '142'      -> wrongly verified
  '8'   is a substring of range '8.0' -> wrongly verified
  '35'  is a substring of '135'       -> wrongly verified
The value must instead match a TOKEN (or a contiguous run of tokens) of the
quote, with numeric tolerance for trailing-zero decimals ('8.4' == '8.40') but
NOT collapsing an integer into a range bound ('8' != '8.0').

These are unit tests over `_value_in_quote` directly (no PDF, no DB) — the
smallest surface that isolates the substring-vs-token defect.
"""

from __future__ import annotations

from app.ingest.schemas import ExtractedField
from app.ingest.verify_extraction import _value_in_quote


def _field(value, quote: str) -> ExtractedField:
    return ExtractedField(value=value, page=1, verbatim_quote=quote, status="extracted")


# --- ATTACK: substring-but-not-a-token misreads must FAIL verification ----------


def test_digit_drop_substring_of_larger_number_fails():
    # '14' is a substring of the genuine '142' but is not a token of the quote.
    assert _value_in_quote(_field("14", "Glucose 142 H mg/dL 70-99")) is False


def test_bare_integer_matching_range_bound_fails():
    # '8' must not verify against the '8.0' upper bound of a reference range.
    assert _value_in_quote(_field("8", "Potassium 4.2 mmol/L 3.5-8.0")) is False


def test_prefix_digit_drop_fails():
    # '35' is a substring of '135' but a different value.
    assert _value_in_quote(_field("35", "Sodium 135 mmol/L 135-145")) is False


# --- LEGIT: real values (exact / decimal / multi-token) still VERIFY ------------


def test_exact_integer_value_verifies():
    assert _value_in_quote(_field("142", "Glucose 142 H mg/dL 70-99")) is True


def test_trailing_zero_decimal_tolerance_verifies():
    # '8.40' is numerically the printed '8.4'.
    assert _value_in_quote(_field("8.40", "Hemoglobin A1c 8.4 % 4.0-5.6")) is True


def test_exact_decimal_value_verifies():
    assert _value_in_quote(_field("0.9", "Creatinine 0.9 mg/dL 0.60-1.30")) is True


def test_multitoken_unit_verifies_as_contiguous_tokens():
    assert _value_in_quote(_field("mg/dL", "Glucose 142 H mg/dL 70-99")) is True


def test_reference_range_verifies_as_contiguous_tokens():
    assert _value_in_quote(_field("70-99", "Glucose 142 H mg/dL 70-99")) is True


def test_symbol_only_unit_percent_verifies():
    # A pure-symbol value ('%') has no alphanumeric token — literal presence is
    # the only sound check, and symbols cannot digit-drop.
    assert _value_in_quote(_field("%", "Hemoglobin A1c 8.4 % 4.0-5.6")) is True


def test_multiword_name_verifies():
    assert _value_in_quote(_field("Hemoglobin A1c", "Hemoglobin A1c 8.4 % 4.0-5.6")) is True


def test_boolean_flag_is_exempt():
    assert _value_in_quote(_field(True, "Glucose 142 H mg/dL 70-99")) is True


# --- medication_list boundary cases (T4): the same contiguous-token rule --------


def test_medlist_strength_digit_drop_fails():
    # '10 mg' against a row that prints only '100 mg': 10 is a substring of 100
    # but not a token — a digit-dropped strength must NOT verify.
    assert _value_in_quote(
        _field("10 mg", "Sertraline 100 mg oral once daily Active Chen")
    ) is False


def test_medlist_expanded_abbreviation_fails():
    # The document prints 'BID'; a model that normalizes to 'twice daily' is
    # asserting text the page does not contain — failed_verification.
    assert _value_in_quote(
        _field("twice daily", "Metformin 1000 mg oral BID Active Chen")
    ) is False


def test_medlist_verbatim_strength_and_frequency_verify():
    quote = "Metformin 1000 mg oral BID Active Chen"
    assert _value_in_quote(_field("1000 mg", quote)) is True
    assert _value_in_quote(_field("BID", quote)) is True


def test_iter_envelopes_covers_every_medlist_subfield():
    """FAILURE MODE GUARDED: a subfield missing from iter_envelopes would skip
    text-layer verification entirely and persist unverified (the exact seam the
    third doc type adds to the pipeline core)."""
    from app.ingest.schemas import MedicationListExtraction, MedicationListItem
    from app.ingest.verify_extraction import iter_envelopes

    ext = MedicationListExtraction(
        medications=[MedicationListItem(field_id="metformin")]
    )
    triples = iter_envelopes(ext)
    assert [(fid, sub) for fid, sub, _env in triples] == [
        ("patient_name", ""),
        ("list_date", ""),
        ("metformin", "name"),
        ("metformin", "strength"),
        ("metformin", "route"),
        ("metformin", "frequency"),
        ("metformin", "status"),
        ("metformin", "prescriber"),
    ]
