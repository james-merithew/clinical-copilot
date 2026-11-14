"""PDF text-layer access — the deterministic authority for extraction claims.

Everything the verifier trusts comes from here, not from the VLM:
- quote search against the REAL text layer (per page, whitespace-normalized),
  returning the page + a normalized 0-1 bounding box built from pdfium char
  rects (`BBox.origin="text_layer"` — the solid-border, trusted overlay);
- page rendering to PNG for the click-to-source viewer (no pdf.js, D7);
- page count / dimensions / has-text-layer detection (a scanned document with
  no text layer can never verify, so nothing from it is persisted — D3.6).

pypdfium2 only; no OCR. Coordinates: pdfium uses bottom-left origin in PDF
points; we emit top-left-origin fractions of the page size so the UI can
position percentage divs directly (W2_ARCHITECTURE.md §5.3).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import pypdfium2 as pdfium

from ..contracts import BBox

_WS_RE = re.compile(r"\s+")


def _normalize_with_map(raw: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to single spaces, returning the normalized
    string plus a map: normalized index -> raw index (whitespace maps to the
    first raw whitespace char of its run). Leading/trailing whitespace dropped.
    """
    out: list[str] = []
    idx_map: list[int] = []
    in_ws = False
    for i, ch in enumerate(raw):
        if ch.isspace():
            if not in_ws and out:  # collapse; drop leading whitespace
                out.append(" ")
                idx_map.append(i)
            in_ws = True
        else:
            out.append(ch)
            idx_map.append(i)
            in_ws = False
    # drop a trailing collapsed space
    while out and out[-1] == " ":
        out.pop()
        idx_map.pop()
    return "".join(out), idx_map


def normalize_text(raw: str) -> str:
    """Whitespace-normalized form used for quote/value containment checks."""
    return _WS_RE.sub(" ", raw).strip()


@dataclass(frozen=True)
class PageInfo:
    page: int  # 1-based
    width: float  # PDF points
    height: float


@dataclass(frozen=True)
class QuoteMatch:
    """A deterministic hit for a quote in the real text layer.

    `exact` = the quote matched the raw text byte-for-byte (confidence tier
    >=0.9); False = it matched only after whitespace normalization (0.7 tier).
    Either way the bbox is authoritative (origin="text_layer").
    """

    page: int
    bbox: BBox
    exact: bool


class PdfTextIndex:
    """Opens a PDF once and serves text/search/geometry for every field check.

    Use as a context manager; verification of a whole extraction shares one
    index so a 12-field document does not re-parse the PDF 12 times.
    """

    def __init__(self, pdf_bytes: bytes) -> None:
        self._pdf = pdfium.PdfDocument(pdf_bytes)
        self._texts: dict[int, str] = {}  # 1-based page -> raw text

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        try:
            self._pdf.close()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass

    def __enter__(self) -> "PdfTextIndex":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- geometry / text -------------------------------------------------------

    @property
    def page_count(self) -> int:
        return len(self._pdf)

    def page_info(self, page: int) -> PageInfo:
        p = self._pdf[page - 1]
        w, h = p.get_size()
        return PageInfo(page=page, width=float(w), height=float(h))

    def page_infos(self) -> list[PageInfo]:
        return [self.page_info(n) for n in range(1, self.page_count + 1)]

    def text(self, page: int) -> str:
        """Raw text layer of a 1-based page ('' when none)."""
        if page in self._texts:
            return self._texts[page]
        if not (1 <= page <= self.page_count):
            return ""
        tp = self._pdf[page - 1].get_textpage()
        try:
            n = tp.count_chars()
            raw = tp.get_text_range(0, n) if n > 0 else ""
        finally:
            tp.close()
        self._texts[page] = raw
        return raw

    def has_text_layer(self) -> bool:
        """True if ANY page carries non-whitespace text. A scanned/image-only
        document returns False → the needs_review path (nothing persisted)."""
        return any(self.text(n).strip() for n in range(1, self.page_count + 1))

    # -- quote search ----------------------------------------------------------

    def find_quote(
        self, page: int, quote: str, value: str | None = None
    ) -> QuoteMatch | None:
        """Find `quote` in the text layer of the CLAIMED page only.

        Two tiers (D3): raw exact substring (`exact=True`), else a
        whitespace-normalized (and finally casefolded) match (`exact=False`).
        Not found on that page -> None -> the field fails verification closed.

        When the quote occurs MORE THAN ONCE on the page, binding the bbox to the
        first occurrence can highlight the wrong region. If `value` is supplied,
        prefer the occurrence whose surrounding text contains the value token;
        if none disambiguates (or `value` is absent) the first occurrence is used.
        """
        if not quote or not quote.strip():
            return None
        raw = self.text(page)
        if not raw.strip():
            return None

        # Tier 1: exact raw substring. Prefer, among all occurrences, the one
        # whose surrounding window contains the value token.
        i = self._best_occurrence(raw, quote, value)
        if i >= 0:
            span = range(i, i + len(quote))
            bbox = self._bbox_for_span(page, list(span))
            if bbox is not None:
                return QuoteMatch(page=page, bbox=bbox, exact=True)

        # Tier 2: whitespace-normalized (then casefolded) match.
        norm_raw, idx_map = _normalize_with_map(raw)
        norm_q = normalize_text(quote)
        if not norm_q:
            return None
        j = norm_raw.find(norm_q)
        if j < 0:
            j = norm_raw.casefold().find(norm_q.casefold())
        if j < 0:
            return None
        raw_indices = idx_map[j : j + len(norm_q)]
        bbox = self._bbox_for_span(page, raw_indices)
        if bbox is None:
            return None
        return QuoteMatch(page=page, bbox=bbox, exact=False)

    @staticmethod
    def _best_occurrence(raw: str, quote: str, value: str | None) -> int:
        """Index of the occurrence of `quote` in `raw` to anchor on.

        Default is the first occurrence. When `quote` appears more than once and
        a disambiguating `value` token is given, prefer the first occurrence
        whose surrounding window (±64 chars) contains that token — so a repeated
        quote does not bind the bbox to an unrelated earlier row.
        """
        first = raw.find(quote)
        if first < 0:
            return -1
        second = raw.find(quote, first + 1)
        if second < 0:
            return first  # unique — nothing to disambiguate

        token = normalize_text(value).casefold() if value else ""
        if not token:
            return first
        # Disambiguate on the matched LINE only (bounded by newlines). A ±char
        # window would bleed into adjacent rows and defeat the purpose.
        i = first
        while i >= 0:
            line_start = raw.rfind("\n", 0, i) + 1
            line_end = raw.find("\n", i)
            if line_end < 0:
                line_end = len(raw)
            line = raw[line_start:line_end]
            if token in normalize_text(line).casefold():
                return i
            i = raw.find(quote, i + 1)
        return first

    def _bbox_for_span(self, page: int, raw_indices: list[int]) -> BBox | None:
        """Union of pdfium char boxes over a raw-index span, as a normalized
        top-left-origin BBox. Whitespace/degenerate boxes are skipped."""
        p = self._pdf[page - 1]
        w, h = p.get_size()
        if not w or not h:
            return None
        raw = self.text(page)
        tp = p.get_textpage()
        try:
            x0 = y0 = float("inf")
            x1 = y1 = float("-inf")
            found = False
            for i in raw_indices:
                if i >= len(raw) or raw[i].isspace():
                    continue
                left, bottom, right, top = tp.get_charbox(i)
                if right <= left or top <= bottom:
                    continue
                found = True
                x0 = min(x0, left)
                y0 = min(y0, bottom)
                x1 = max(x1, right)
                y1 = max(y1, top)
        finally:
            tp.close()
        if not found:
            return None

        def _clamp(v: float) -> float:
            return max(0.0, min(1.0, v))

        return BBox(
            page=page,
            x0=_clamp(x0 / w),
            y0=_clamp((h - y1) / h),  # top-left origin
            x1=_clamp(x1 / w),
            y1=_clamp((h - y0) / h),
            origin="text_layer",
        )


# ---- standalone helpers ------------------------------------------------------


def page_count(pdf_bytes: bytes) -> int:
    with PdfTextIndex(pdf_bytes) as idx:
        return idx.page_count


def render_page_png(pdf_bytes: bytes, page: int, scale: float = 2.0) -> bytes:
    """Render one 1-based page to PNG bytes (the `<img>` behind the bbox
    overlay). Scale is clamped to a sane range server-side by the route."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        if not (1 <= page <= len(pdf)):
            raise ValueError("page out of range")
        bitmap = pdf[page - 1].render(scale=scale)
        pil_image = bitmap.to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()
