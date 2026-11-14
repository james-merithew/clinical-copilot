"""pdf_text: the deterministic text-layer authority (D3).

Failure modes guarded:
- a wrong bbox would highlight the WRONG region in the click-to-source viewer,
  silently eroding the provenance story (bbox tolerance test vs the known
  reportlab layout of lab_simple.pdf);
- a scanned document misreported as having a text layer would let unverifiable
  fields masquerade as verified (needs-review path depends on this signal).
"""

from __future__ import annotations

from app.ingest.pdf_text import PdfTextIndex, render_page_png

from _helpers import LAB_SCANNED_PDF, LAB_SIMPLE_PDF

# lab_simple.pdf layout ground truth (make_fixtures.py, letter = 612x792pt):
# the Glucose row baseline sits at y=545pt; the RESULT column ("142") starts at
# x = 0.75in + 2.7in = 248.4pt. Normalized, top-left origin.
_EXPECT_142_X0 = 248.4 / 612.0
_EXPECT_142_Y1 = (792.0 - 545.0) / 792.0  # bottom edge of the glyphs
_TOL = 0.05


def test_find_quote_exact_bbox_within_tolerance():
    with PdfTextIndex(LAB_SIMPLE_PDF) as idx:
        match = idx.find_quote(1, "142")
    assert match is not None
    assert match.exact is True
    bb = match.bbox
    assert bb.origin == "text_layer"
    assert bb.page == 1
    assert abs(bb.x0 - _EXPECT_142_X0) <= _TOL
    assert abs(bb.y1 - _EXPECT_142_Y1) <= _TOL
    # sane geometry: a small box, x0<x1, y0<y1, inside the page
    assert 0.0 <= bb.x0 < bb.x1 <= 1.0
    assert 0.0 <= bb.y0 < bb.y1 <= 1.0
    assert (bb.y1 - bb.y0) < 0.05  # one text line, not half the page


def test_find_quote_row_line_and_normalized_match():
    with PdfTextIndex(LAB_SIMPLE_PDF) as idx:
        # whole row as the VLM would quote it (runs joined by single spaces)
        row = idx.find_quote(1, "Glucose 142 H mg/dL 70-99")
        assert row is not None
        # extra internal whitespace only matches after normalization
        norm = idx.find_quote(1, "Glucose   142")
        assert norm is not None and norm.exact is False
        # wrong page -> no match (page-scoped search is the point)
        assert idx.find_quote(2, "Glucose") is None
        # invented text -> no match
        assert idx.find_quote(1, "Troponin I 12.3 ng/mL") is None


def _dup_token_pdf() -> bytes:
    """A page where the quote token 'mg/dL' appears TWICE: first in a header
    row (higher on the page), then in the data row that actually carries '142'.
    The correct occurrence for the glucose value is NOT the first one."""
    import io

    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 720, "Analyte Result Units mg/dL Reference")  # header (first)
    c.drawString(72, 660, "Glucose 142 mg/dL 70-99")  # data row (has 142)
    c.showPage()
    c.save()
    return buf.getvalue()


def test_find_quote_prefers_occurrence_near_value_token():
    pdf = _dup_token_pdf()
    with PdfTextIndex(pdf) as idx:
        # No hint -> first occurrence (the header row, higher on the page).
        first = idx.find_quote(1, "mg/dL")
        # With the value hint, the occurrence in the row that also contains
        # '142' (the data row, lower on the page) must be chosen instead.
        best = idx.find_quote(1, "mg/dL", value="142")
    assert first is not None and best is not None
    # top-left origin: the data row sits lower -> larger y0 than the header.
    assert best.bbox.y0 > first.bbox.y0
    # sanity: an ambiguous hint that matches neither window keeps first.
    with PdfTextIndex(pdf) as idx:
        fallback = idx.find_quote(1, "mg/dL", value="99999")
    assert fallback is not None
    assert abs(fallback.bbox.y0 - first.bbox.y0) < 1e-6


def test_scanned_pdf_has_no_text_layer():
    with PdfTextIndex(LAB_SCANNED_PDF) as idx:
        assert idx.has_text_layer() is False
        assert idx.find_quote(1, "Glucose") is None
    with PdfTextIndex(LAB_SIMPLE_PDF) as idx:
        assert idx.has_text_layer() is True


def test_render_page_png_returns_png_bytes():
    png = render_page_png(LAB_SIMPLE_PDF, 1, scale=1.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000
