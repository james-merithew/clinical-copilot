"""FastAPI sidecar — /health, /ready, /chat, and the served chat UI.

Trust boundary highlights:
- Every patient/ops request carries a short-lived **session token** minted by
  OpenEMR after a real `aclCheckCore` (AUTH_TOKEN_SPEC.md, session_token.py).
  The sidecar VERIFIES it (HMAC), then **binds** it: patient endpoints require
  `scope=="patient"` AND `claims.pid == the request pid`, so a token for
  patient A can never act on patient B. Identity (`user_id`) comes from the
  token's `sub`, never from the client and never from `demo_user_id`.
- The token's existence proves the OpenEMR ACL passed at mint; `authorize()`
  then composes the per-patient (provider-match) SCOPE on top. Both must pass.
- Per-IP rate limited. The token is a bearer credential and is NEVER logged.
- A global exception handler logs only the exception TYPE + correlation id — never
  PHI or DB error strings.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from datetime import date as _date

from . import db
from . import session_token
from .agent import run_turn
from .authz import authorize
from .config import get_settings
from .session_token import Claims
from .contracts import (
    AgentResponse,
    AuthzDenied,
    ChatRequest,
    ChipIntent,
    DayAppointment,
    DayResponse,
    FlagSummary,
    LabTrendResponse,
    SnapshotResponse,
)
from . import metrics_summary
from .metrics import build_metrics
from .tools.demographics import get_demographics
from .tools.encounters import get_care_transitions
from .tools.labs import get_labs
from .tools.medications import get_medications
from .tools.problems import get_problems_allergies
from .tools.vitals import get_vitals
from .verifier import _check_constraints
from .correlation import (
    configure_logging,
    get_correlation_id,
    log_event,
    new_correlation_id,
    set_correlation_id,
)
from .observability import TurnObserver, configure_observability, ls_ready
from .tools import ToolContext

log = logging.getLogger("copilot.main")

UI_PATH = Path(__file__).resolve().parent.parent / "ui" / "index.html"

# Week-3 hardening: the interactive API explorer (/docs, /redoc) and the machine schema
# (/openapi.json) are DISABLED by default — an unauthenticated caller must not be able to
# enumerate the full attack surface of a PHI-adjacent service. A dev/demo opts back in with
# COPILOT_EXPOSE_DOCS=true. `app.openapi()` (the method the export script uses) is
# unaffected, so the committed openapi.json + its drift test still work.
_startup_settings = get_settings()
app = FastAPI(
    title="Clinical Co-Pilot",
    version="0.1.0",
    docs_url="/docs" if _startup_settings.expose_docs else None,
    redoc_url="/redoc" if _startup_settings.expose_docs else None,
    openapi_url="/openapi.json" if _startup_settings.expose_docs else None,
)

# --- simple per-IP rate limit (dependency; avoids slowapi's route quirks) -----
_RL_WINDOW = 60
_RL: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    limit = get_settings().rate_limit_per_min
    ip = request.client.host if request.client else "?"
    now = time.time()
    dq = _RL[ip]
    while dq and dq[0] < now - _RL_WINDOW:
        dq.popleft()
    if len(dq) >= limit:
        raise HTTPException(status_code=429, detail="rate_limited")
    dq.append(now)


@app.on_event("startup")
def _startup() -> None:
    configure_logging()
    configure_observability()
    log_event(log, "startup", **get_settings().resolution_summary())


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):  # noqa: ANN001
    # PHI-safe: never leak the exception message / DB row data.
    log_event(log, "unhandled_error", path=request.url.path, error=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal_error", "correlation_id": get_correlation_id()},
    )


def _metrics_path(request: Request) -> str:
    """The PHI-free metrics key for a request: the matched route TEMPLATE
    (`/documents/{document_id}/page/{page}.png`), never the raw path with ids.
    Unmatched requests (no route in scope) bucket under a single "<unmatched>"
    key — a caller can put ids/PHI in an arbitrary URL, and verbatim keys would
    both leak that into the "PHI-free" summary and blow up its cardinality."""
    route = request.scope.get("route")
    return getattr(route, "path_format", None) or getattr(route, "path", None) or "<unmatched>"


@app.middleware("http")
async def _correlation(request: Request, call_next):  # noqa: ANN001
    cid = request.headers.get("x-correlation-id") or new_correlation_id()
    set_correlation_id(cid)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        # An unhandled error must NOT be invisible: record it as a 5xx (so the
        # health dashboard reflects the outage) and still return the correlation
        # id header (so the failure is traceable). PHI-safe: log only the type.
        metrics_summary.record_request(
            path=_metrics_path(request), method=request.method,
            status=500, ms=(time.perf_counter() - t0) * 1000.0,
        )
        log_event(log, "unhandled_error", path=request.url.path, error=type(exc).__name__)
        resp = JSONResponse(
            status_code=500,
            content={"detail": "internal_error", "correlation_id": cid},
        )
        resp.headers["x-correlation-id"] = cid
        return resp
    response.headers["x-correlation-id"] = cid
    # PHI-free request metrics (D6b dashboard): route TEMPLATE, status, latency.
    metrics_summary.record_request(
        path=_metrics_path(request),
        method=request.method,
        status=response.status_code,
        ms=(time.perf_counter() - t0) * 1000.0,
    )
    return response


def require_session_token(request: Request) -> Claims:
    """Verify the Bearer session token and return its claims. 401 on any
    failure. NEVER logs the token — only the failure reason + path."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        key = session_token.get_signing_key()
        return session_token.verify(token, key, int(time.time()))
    except session_token.TokenInvalid:
        # PHI/secret-safe: log the reason enum + path, never the token itself.
        log_event(log, "auth_reject", reason="token_invalid", path=request.url.path)
        raise HTTPException(status_code=401, detail="unauthorized")


def _has_valid_session(request: Request) -> bool:
    """True if the caller presented a valid session token — used to gate detailed
    readiness output without turning /ready into an authenticated endpoint (it stays a
    200/503 probe for everyone; only the component inventory is withheld from anonymous)."""
    try:
        require_session_token(request)
        return True
    except HTTPException:
        return False


def _deny_patient(cid: str, reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content=AuthzDenied(
            correlation_id=cid,
            detail=f"The requesting user is not authorized to view this patient ({reason}).",
        ).model_dump(),
    )


def enforce_patient(claims: Claims, pid: int, cid: str) -> JSONResponse | None:
    """Bind a verified token to a patient request. Returns a 403 JSONResponse to
    short-circuit, or None when both authorization layers pass.

    Layer 1 (token binding): scope must be "patient", the token's pid must match
    the request pid, and the ACL asserted at mint must be "patients:demo".
    Layer 2 (per-patient scope): authorize(claims.user_id, pid) — provider match.
    The ACL/role decision itself was already made by OpenEMR gacl at mint."""
    if claims.scope != "patient" or int(claims.pid) != int(pid):
        return _deny_patient(cid, "token_patient_mismatch")
    if claims.acl != "patients:demo":
        return _deny_patient(cid, "unexpected_acl")
    decision = authorize(claims.user_id, pid)
    if not decision.allowed:
        return _deny_patient(cid, decision.reason)
    return None


# --- health / readiness ------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "alive"}


def _llm_ready(s) -> bool:  # noqa: ANN001
    if s.mock_llm:
        return True
    if not s.anthropic_api_key:
        return False
    try:  # cheap GET, not a completion
        import anthropic

        anthropic.Anthropic(api_key=s.anthropic_api_key).models.list(limit=1)
        return True
    except Exception as exc:  # noqa: BLE001
        log_event(log, "llm_unreachable", error=type(exc).__name__)
        return False


@app.get("/ready")
def ready(request: Request):
    s = get_settings()
    checks = {"database": db.ping(), "llm": _llm_ready(s), "observability": ls_ready()}
    ok = all(checks.values())
    status = "ready" if ok else "down"
    # Week 2 (W2_ARCHITECTURE §9.7): ancillary dependencies are DEGRADED
    # (200 + detail), never a binary flip — chat continues without them.
    # Rubric checks: vector_index + reranker always; document_storage (like
    # openemr_api / ingest_db) only when ingestion is enabled.
    from .rag import index as _rag_index
    from .rag import providers as _rag_providers

    checks["vector_index"] = _rag_index.vector_index_ready()
    checks["reranker"] = _rag_providers.reranker_ready(s)
    ancillary_ok = checks["vector_index"] and checks["reranker"]
    if s.ingest_enabled:
        from .ingest import openemr_api as _openemr_api
        from .ingest import writer as _ingest_writer

        checks["openemr_api"] = _openemr_api.ping()
        checks["ingest_db"] = _ingest_writer.ping()
        checks["document_storage"] = _openemr_api.document_storage_ready(s)
        ancillary_ok = ancillary_ok and all(
            (checks["openemr_api"], checks["ingest_db"], checks["document_storage"])
        )
    if ok and not ancillary_ok:
        status = "degraded"
    body = {
        "ready": ok,
        "status": status,
        "correlation_id": get_correlation_id(),
    }
    # Week-3 hardening (remediates RT-005): the internal component inventory — which
    # backing subsystems exist and their health — is reconnaissance for an attacker, so
    # it is disclosed ONLY to an authorized caller (valid session token) or when the
    # demo flag COPILOT_READY_PUBLIC_DETAIL=true is set. Anonymous probes still get the
    # correct 200/503 + coarse status ("ready"/"degraded"/"down"), so Railway health
    # checks and monitors are unaffected. The full Week-2 rubric detail remains available
    # to authenticated callers — the observability was not removed, just scoped.
    if s.ready_public_detail or _has_valid_session(request):
        body["checks"] = checks
    return JSONResponse(status_code=200 if ok else 503, content=body)


# --- chat --------------------------------------------------------------------

_CHIP_TEXT = {
    ChipIntent.explain_care_transition: "Why was the patient at that encounter — what happened and what changed?",
    ChipIntent.lab_trend: "Show the recent trend for the relevant lab values.",
    ChipIntent.med_detail: "Give the details of the patient's current medications.",
    ChipIntent.care_gaps: "What care gaps, overdue screenings, or can't-miss items are there for this patient today?",
}


@app.post("/chat", response_model=None)
def chat(
    request: Request,
    body: ChatRequest,
    claims: Claims = Depends(require_session_token),
    _rl: None = Depends(rate_limit),
):
    cid = get_correlation_id()
    denied = enforce_patient(claims, body.pid, cid)
    if denied is not None:
        return denied
    user_id = claims.user_id  # identity from the OpenEMR-minted token, not a pin

    kind = "chip" if body.chip_intent else "message"
    obs = TurnObserver(pid=body.pid, kind=kind)
    ctx = ToolContext(correlation_id=cid, user_id=user_id, pid=body.pid)
    user_text = _CHIP_TEXT.get(body.chip_intent) if body.chip_intent else body.message
    history = [t.model_dump() for t in body.history]

    try:
        resp: AgentResponse = run_turn(ctx, user_text, history, obs)
        return resp
    finally:
        obs.finish()


# --- snapshot / day (deterministic dashboard data, no LLM) -------------------


def _build_snapshot(ctx) -> SnapshotResponse:  # noqa: ANN001
    demo = None
    problems: list = []
    allergies: list = []
    meds: list = []
    labs: list = []
    degraded = False
    notes: list[str] = []
    try:
        demo = get_demographics(ctx)
    except Exception:  # noqa: BLE001
        degraded = True
        notes.append("demographics unavailable")
    try:
        pa = get_problems_allergies(ctx)
        problems, allergies = pa.problems, pa.allergies
    except Exception:  # noqa: BLE001
        degraded = True
        notes.append("problems/allergies unavailable")
    try:
        meds = get_medications(ctx).medications
    except Exception:  # noqa: BLE001
        degraded = True
        notes.append("medications unavailable")
    try:
        labs = get_labs(ctx).labs
    except Exception:  # noqa: BLE001
        degraded = True
        notes.append("labs unavailable")
    vitals: list = []
    try:
        vitals = get_vitals(ctx).vitals
    except Exception:  # noqa: BLE001
        notes.append("vitals unavailable")
    encounters: list = []
    try:
        encounters = get_care_transitions(ctx).encounters
    except Exception:  # noqa: BLE001
        notes.append("encounters unavailable")

    labs_by_test: dict[str, list] = {}
    for lab in labs:
        labs_by_test.setdefault(lab.name, []).append(lab)
    for k in list(labs_by_test):  # chronological + capped for the sparkline
        labs_by_test[k] = sorted(labs_by_test[k], key=lambda x: x.date or "")[-8:]

    metrics = build_metrics(vitals, labs)
    flags = _check_constraints(meds, allergies, labs, vitals)
    return SnapshotResponse(
        correlation_id=get_correlation_id(),
        demographics=demo,
        metrics=metrics,
        encounters=encounters[:5],
        problems=problems,
        medications=meds,
        allergies=allergies,
        labs_by_test=labs_by_test,
        constraint_flags=flags,
        degraded=degraded,
        notes=("; ".join(notes) or None),
    )


@app.get("/snapshot", response_model=None)
def snapshot(
    request: Request,
    pid: int,
    claims: Claims = Depends(require_session_token),
    _rl: None = Depends(rate_limit),
):
    cid = get_correlation_id()
    denied = enforce_patient(claims, pid, cid)
    if denied is not None:
        return denied
    user_id = claims.user_id
    obs = TurnObserver(pid=pid, kind="snapshot")
    try:
        with obs.step("snapshot"):
            return _build_snapshot(ToolContext(cid, user_id, pid))
    finally:
        obs.finish()


def _build_lab_trend(ctx, test: str | None = None) -> LabTrendResponse:  # noqa: ANN001
    """Group `get_labs` rows by test name, each series chronological ASC — the
    shape the chart widget plots. Every point keeps its procedure_result
    SourceRef (citation invariant). A labs failure degrades to an explicit
    empty-series response (never a 500, never a fabricated point)."""
    try:
        result = get_labs(ctx, name=test)
    except Exception:  # noqa: BLE001
        return LabTrendResponse(
            correlation_id=get_correlation_id(),
            test=test,
            notes="labs unavailable",
        )
    series: dict[str, list] = {}
    for lab in result.labs:
        series.setdefault(lab.name, []).append(lab)
    for k in series:
        series[k].sort(key=lambda x: x.date or "")
    return LabTrendResponse(
        correlation_id=get_correlation_id(),
        test=test,
        series=series,
        truncated=result.truncated,
    )


@app.get("/labs/trend", response_model=None)
def labs_trend(
    request: Request,
    pid: int,
    test: str | None = None,
    claims: Claims = Depends(require_session_token),
    _rl: None = Depends(rate_limit),
):
    """Deterministic lab-trend series for the chart widget — no LLM. Same
    two-layer authorization as /snapshot: token binding + per-patient scope."""
    cid = get_correlation_id()
    denied = enforce_patient(claims, pid, cid)
    if denied is not None:
        return denied
    obs = TurnObserver(pid=pid, kind="lab_trend")
    try:
        with obs.step("lab_trend"):
            return _build_lab_trend(ToolContext(cid, claims.user_id, pid), test)
    finally:
        obs.finish()


def _build_day(date_str: str, provider: int) -> DayResponse:
    cid = get_correlation_id()
    rows = db.query(
        """
        SELECT pc_pid AS pid, TIME_FORMAT(pc_startTime, '%H:%i') AS t,
               pc_hometext AS reason, pc_title AS title
        FROM openemr_postcalendar_events
        WHERE pc_eventDate = :d AND pc_aid = :aid AND pc_pid IS NOT NULL AND pc_pid > 0
        ORDER BY pc_startTime
        """,
        {"d": date_str, "aid": provider},
    )
    appts: list[DayAppointment] = []
    for r in rows:
        pid = int(r["pid"])
        if not authorize(provider, pid).allowed:  # only the provider's own patients
            continue
        ctx = ToolContext(cid, provider, pid)
        try:
            demo = get_demographics(ctx)
        except Exception:  # noqa: BLE001
            demo = None
        crit = warn = 0
        changed = False
        try:
            meds = get_medications(ctx).medications
            pa = get_problems_allergies(ctx)
            labs = get_labs(ctx).labs
            flags = _check_constraints(meds, pa.allergies, labs)
            crit = sum(1 for f in flags if f.severity.value == "critical")
            warn = sum(1 for f in flags if f.severity.value == "warning")
        except Exception:  # noqa: BLE001
            pass
        try:
            encs = get_care_transitions(ctx).encounters
            changed = any(e.kind in ("ed", "inpatient") for e in encs[:3])
        except Exception:  # noqa: BLE001
            pass
        appts.append(
            DayAppointment(
                time=r["t"] or "",
                pid=pid,
                demographics=demo,
                reason=(r["reason"] or r["title"] or None),
                flags=FlagSummary(critical=crit, warning=warn),
                changed_recently=changed,
            )
        )
    appts.sort(key=lambda a: (-(a.flags.critical * 2 + a.flags.warning), a.time))
    return DayResponse(
        correlation_id=cid, date=date_str, provider_id=provider, count=len(appts), appointments=appts
    )


@app.get("/day", response_model=None)
def day(
    request: Request,
    date: str | None = None,
    claims: Claims = Depends(require_session_token),
    _rl: None = Depends(rate_limit),
):
    if claims.scope != "day":
        return JSONResponse(
            status_code=403,
            content=AuthzDenied(
                correlation_id=get_correlation_id(),
                detail="This token is not authorized for the day view.",
            ).model_dump(),
        )
    obs = TurnObserver(pid=0, kind="day")
    try:
        with obs.step("day"):
            return _build_day(date or _date.today().isoformat(), claims.user_id)
    finally:
        obs.finish()


# --- Week 2: document ingestion (Lane A) — routes live in ingest_routes.py ----

from .ingest_routes import router as _ingest_router  # noqa: E402

app.include_router(_ingest_router)


# --- Week 2: PHI-free metrics + dashboard (D6b, W2_ARCHITECTURE §9.3) ---------

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "ui" / "dashboard.html"


@app.get("/metrics/summary")
def metrics_summary_endpoint(
    request: Request,
    _claims: Claims = Depends(require_session_token),
    _rl: None = Depends(rate_limit),
) -> dict:
    """In-process counters and rates only — no PHI. Accepts ANY valid session
    token (patient or day scope); headless/CI use a server-side mint."""
    return metrics_summary.summary()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    try:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HTMLResponse("<h1>Dashboard</h1><p>UI not found.</p>", status_code=200)
    # No token is baked into the page. The dashboard JS reads the token from the
    # URL fragment (or /dev/token in dev mode) and sends it to /metrics/summary.
    return HTMLResponse(html)


# --- dev-only token mint (local UI + load tests, gated by auth_dev_mode) ------


@app.get("/dev/token")
def dev_token(scope: str = "patient", pid: int | None = None) -> JSONResponse:
    """Mint a session token locally so the standalone UI + load tests work
    without a running OpenEMR. 404 unless COPILOT_AUTH_DEV_MODE=true — this
    endpoint MUST NOT exist in production."""
    s = get_settings()
    if not s.auth_dev_mode:
        raise HTTPException(status_code=404, detail="not_found")
    if scope not in ("patient", "day"):
        raise HTTPException(status_code=422, detail="bad scope")
    token = session_token.mint(
        sub=s.demo_user_id,
        usr="dev",
        pid=(s.default_pid if scope == "patient" else 0) if pid is None else pid,
        scope=scope,
        acl="patients:demo",
        key=session_token.get_signing_key(),
        ttl=s.token_ttl_s,
        now=int(time.time()),
    )
    return JSONResponse({"token": token, "scope": scope})


# --- served UI ---------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    s = get_settings()
    try:
        html = UI_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HTMLResponse("<h1>Clinical Co-Pilot</h1><p>UI not found.</p>", status_code=200)
    pid = request.query_params.get("pid", "")
    if not pid.isdigit():
        pid = ""
    # No token is baked into the page. The panel JS reads the session token from
    # the URL fragment (#t=<token>) that OpenEMR appends, or /dev/token in dev
    # mode. Only PHI-free embed pid + default pid are injected server-side.
    html = (
        html.replace("__COPILOT_PID__", pid)
        .replace("__COPILOT_DEFAULT_PID__", str(s.default_pid))
    )
    resp = HTMLResponse(html)
    # Allow OpenEMR (and self) to frame this page; never send X-Frame-Options: DENY.
    resp.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://openemr-production-31ad.up.railway.app "
        "http://localhost:* http://127.0.0.1:*"
    )
    return resp
