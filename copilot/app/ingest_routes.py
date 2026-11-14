"""Week 2 ingestion routes (Lane A, D7): upload + document viewer endpoints.

- POST /ingest — multipart upload (file + pid + doc_type). Bearer auth, per-IP
  rate limit, size cap, mimetype allowlist, then the Authorization Gateway
  (user<->patient) BEFORE the pipeline runs. 404 while ingestion is disabled
  so the surface simply does not exist unless configured.
- GET /documents/{id}/meta — page count/dimensions for the viewer.
- GET /documents/{id}/page/{n}.png — server-rendered page image (pypdfium2,
  LRU-cached). No pdf.js: the UI overlays percentage-positioned bbox divs.
- GET /documents/{id}/citations — list[DocumentRef] from copilot.provenance.

Every document endpoint requires `pid` and verifies BOTH the user<->patient
authorization AND that the document belongs to that patient — a document id
is never a capability by itself.
"""

from __future__ import annotations

import base64
import logging
from collections import OrderedDict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import metrics_summary
from .config import get_settings
from .contracts import DocumentRef
from .correlation import get_correlation_id, log_event
from .ingest import pdf_text, provenance, service, writer
from .ingest.openemr_api import OpenEMRClient, OpenEMRError
from .ingest.schemas import IngestResult

log = logging.getLogger("copilot.ingest.routes")

router = APIRouter()

_ALLOWED_MIMETYPES = {"application/pdf", "image/png", "image/jpeg"}
_DOC_TYPES = {"lab_report", "intake_form", "medication_list"}


# --- shared deps (lazy import so main.py stays the single source of truth) ----


def _session_token(request: Request):
    """Verify the Bearer session token; returns Claims (401 on failure)."""
    from .main import require_session_token

    return require_session_token(request)


def _rate(request: Request) -> None:
    from .main import rate_limit

    rate_limit(request)


def _require_enabled() -> None:
    if not get_settings().ingest_enabled:
        # The feature is not configured: the route does not exist.
        raise HTTPException(status_code=404, detail="not_found")


def _authz_or_403(claims, pid: int) -> JSONResponse | None:
    """Bind the verified token to this request's pid, then the per-patient scope.
    The pid comes from the endpoint's path/query/form — a token for patient A can
    never act on patient B. Identity is the token's `sub`, never a server pin."""
    from .main import enforce_patient

    return enforce_patient(claims, pid, get_correlation_id())


# --- POST /ingest ---------------------------------------------------------------


@router.post("/ingest", response_model=None)
async def ingest(
    request: Request,
    file: UploadFile = File(...),
    pid: int = Form(...),
    doc_type: str = Form(...),
    claims=Depends(_session_token),
    _rl: None = Depends(_rate),
):
    _require_enabled()
    s = get_settings()

    if doc_type not in _DOC_TYPES:
        raise HTTPException(
            status_code=422,
            detail="doc_type must be lab_report, intake_form or medication_list",
        )

    mimetype = (file.content_type or "").split(";")[0].strip().lower()
    if mimetype not in _ALLOWED_MIMETYPES:
        raise HTTPException(status_code=415, detail="unsupported media type")

    data = await file.read()
    cap = s.ingest_max_file_mb * 1024 * 1024
    if len(data) > cap:
        raise HTTPException(status_code=413, detail="file too large")
    if not data:
        raise HTTPException(status_code=422, detail="empty file")

    denied = _authz_or_403(claims, pid)
    if denied is not None:
        return denied

    result: IngestResult = service.attach_and_extract(
        pid=pid,
        file_bytes=data,
        filename=file.filename or "",
        mimetype=mimetype,
        doc_type=doc_type,
        correlation_id=get_correlation_id(),
    )
    # D6b dashboard: ingestion count + field-level pass rate (counters only).
    metrics_summary.record_ingest(
        status=result.status,
        deduplicated=result.deduplicated,
        fields_total=result.fields_total,
        fields_extracted=result.fields_extracted,
        fields_failed_verification=result.fields_failed_verification,
    )
    return result


# --- document loading (shared by the viewer endpoints) ---------------------------


def _load_document(pid: int, document_id: int) -> tuple[bytes, str | None]:
    """Fetch document bytes, enforcing document<->patient ownership. DB-stored
    bytes (fallback path) first; else OpenEMR REST download."""
    with writer.get_engine().connect() as conn:
        row = writer.load_document_row(conn, document_id)
    if row is None or int(row.get("foreign_id") or 0) != int(pid):
        raise HTTPException(status_code=404, detail="document not found for this patient")
    if row.get("document_data"):
        try:
            return base64.standard_b64decode(row["document_data"]), row.get("mimetype")
        except Exception:  # noqa: BLE001 - corrupt blob = missing document
            raise HTTPException(status_code=404, detail="document unreadable")
    s = get_settings()
    if s.openemr_base_url:
        try:
            client = OpenEMRClient(settings=s)
            return client.download_document(pid, document_id), row.get("mimetype")
        except OpenEMRError as exc:
            log_event(log, "openemr_api_error", phase="download", error=type(exc).__name__)
            raise HTTPException(status_code=503, detail="document store unavailable")
    raise HTTPException(status_code=404, detail="document bytes unavailable")


# --- GET /documents/{id}/meta -----------------------------------------------------


class PageDim(BaseModel):
    page: int
    width: float
    height: float


class DocumentMeta(BaseModel):
    document_id: int
    pid: int
    mimetype: str | None = None
    page_count: int = 0
    pages: list[PageDim] = []


@router.get("/documents/{document_id}/meta", response_model=None)
def document_meta(
    request: Request,
    document_id: int,
    pid: int,
    claims=Depends(_session_token),
    _rl: None = Depends(_rate),
):
    _require_enabled()
    denied = _authz_or_403(claims, pid)
    if denied is not None:
        return denied
    data, mimetype = _load_document(pid, document_id)
    pages: list[PageDim] = []
    if (mimetype or "").startswith("application/pdf"):
        with pdf_text.PdfTextIndex(data) as idx:
            for info in idx.page_infos():
                pages.append(PageDim(page=info.page, width=info.width, height=info.height))
    return DocumentMeta(
        document_id=document_id,
        pid=pid,
        mimetype=mimetype,
        page_count=len(pages),
        pages=pages,
    )


# --- GET /documents/{id}/page/{n}.png ----------------------------------------------

# Tiny LRU for rendered page PNGs (documents are immutable once stored).
_PAGE_CACHE: OrderedDict[tuple[int, int, float], bytes] = OrderedDict()
_PAGE_CACHE_MAX = 64


def _render_cached(pid: int, document_id: int, page: int, scale: float) -> bytes:
    # Ownership check (document<->patient) runs on EVERY request, BEFORE the
    # cache is consulted. The cache key is (document_id, page, scale) with no
    # patient identity, so a warm hit must NOT be a shortcut around authorization:
    # two patients of the same server-pinned provider both clear user<->patient
    # authz, and this per-document check is the only thing separating them.
    # A warm hit that returned before this ran would leak patient A's rendered
    # page to patient B (cross-patient PHI leak — invariant #2).
    data, mimetype = _load_document(pid, document_id)
    key = (document_id, page, scale)
    if key in _PAGE_CACHE:
        _PAGE_CACHE.move_to_end(key)
        return _PAGE_CACHE[key]
    if not (mimetype or "").startswith("application/pdf"):
        raise HTTPException(status_code=415, detail="page rendering requires a PDF document")
    try:
        png = pdf_text.render_page_png(data, page, scale=scale)
    except ValueError:
        raise HTTPException(status_code=404, detail="page out of range")
    _PAGE_CACHE[key] = png
    while len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
        _PAGE_CACHE.popitem(last=False)
    return png


@router.get("/documents/{document_id}/page/{page}.png", response_model=None)
def document_page_png(
    request: Request,
    document_id: int,
    page: int,
    pid: int,
    scale: float = 2.0,
    claims=Depends(_session_token),
    _rl: None = Depends(_rate),
):
    _require_enabled()
    denied = _authz_or_403(claims, pid)
    if denied is not None:
        return denied
    scale = max(0.5, min(4.0, float(scale)))
    png = _render_cached(pid, document_id, page, scale)
    return Response(content=png, media_type="image/png")


# --- GET /documents/{id}/citations --------------------------------------------------


@router.get("/documents/{document_id}/citations", response_model=None)
def document_citations(
    request: Request,
    document_id: int,
    pid: int,
    claims=Depends(_session_token),
    _rl: None = Depends(_rate),
):
    _require_enabled()
    denied = _authz_or_403(claims, pid)
    if denied is not None:
        return denied
    # Ownership check (also 404s unknown ids) before exposing provenance.
    with writer.get_engine().connect() as conn:
        row = writer.load_document_row(conn, document_id)
        if row is None or int(row.get("foreign_id") or 0) != int(pid):
            raise HTTPException(status_code=404, detail="document not found for this patient")
        refs: list[DocumentRef] = provenance.list_citations(conn, document_id)
    return refs
