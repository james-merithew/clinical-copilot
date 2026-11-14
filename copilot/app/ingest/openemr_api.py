"""OpenEMR REST client — the document-store front door (D2).

The source document goes through OpenEMR's own API
(`POST /api/patient/:pid/document?path=/Lab_Report`) via OAuth2 password
grant, so OpenEMR itself creates the `documents` row and computes its native
sha3-512 content hash. The endpoint returns only true/false; `documents.id`
is recovered by hash SELECT (writer.find_document_id_by_hash).

Budgets (W2_ARCHITECTURE §9.2): 10s timeout, 2 retries — safe because the
sha3-512 dedup makes a re-store idempotent. Failures raise TYPED errors so
the service can distinguish an auth outage (`oauth_keys` gap, top risk #1 →
flag-gated DB fallback) from plain unavailability. All failures log
`openemr_api_error` with the error TYPE only — never URLs-with-PHI, never
filenames, never document bytes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..correlation import log_event

log = logging.getLogger("copilot.ingest.openemr_api")

_TIMEOUT_S = 10.0
_RETRIES = 2

_CATEGORY_PATH = {
    "lab_report": "/Lab Report",
    "intake_form": "/Patient Information",
    # Med lists file under the stock "Patient Information" category too: it is
    # patient/pharmacy-reported information, and OpenEMR ships no default
    # "Medication List" document category to target.
    "medication_list": "/Patient Information",
}


class OpenEMRError(Exception):
    """Base for OpenEMR REST failures."""


class OpenEMRAuthError(OpenEMRError):
    """Token fetch / 401 — typically the oauth_keys gap (runbook + fallback)."""


class OpenEMRUnavailable(OpenEMRError):
    """Transport failure / 5xx after retries."""


class OpenEMRClient:
    """Minimal OAuth2-password-grant client with token caching.

    `transport` is injectable for hermetic tests (httpx.MockTransport).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._s = settings or get_settings()
        self._client = httpx.Client(
            base_url=self._s.openemr_base_url.rstrip("/"),
            timeout=_TIMEOUT_S,
            transport=transport,
        )
        self._token: str | None = None
        self._token_expiry: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._s.openemr_base_url and self._s.openemr_oauth_client_id)

    def close(self) -> None:
        self._client.close()

    # -- OAuth2 ----------------------------------------------------------------

    def _fetch_token(self) -> str:
        data = {
            "grant_type": "password",
            "client_id": self._s.openemr_oauth_client_id,
            "scope": "openid api:oemr user/document.write user/document.read",
            "user_role": "users",
            "username": self._s.openemr_api_username,
            "password": self._s.openemr_api_password,
        }
        if self._s.openemr_oauth_client_secret:
            data["client_secret"] = self._s.openemr_oauth_client_secret
        try:
            resp = self._client.post("/oauth2/default/token", data=data)
        except httpx.HTTPError as exc:
            log_event(log, "openemr_api_error", phase="token", error=type(exc).__name__)
            raise OpenEMRUnavailable(type(exc).__name__) from exc
        if resp.status_code in (400, 401, 403):
            log_event(log, "openemr_api_error", phase="token", error=f"http_{resp.status_code}")
            raise OpenEMRAuthError(f"token endpoint returned {resp.status_code}")
        if resp.status_code >= 500:
            log_event(log, "openemr_api_error", phase="token", error=f"http_{resp.status_code}")
            raise OpenEMRUnavailable(f"token endpoint returned {resp.status_code}")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise OpenEMRAuthError("token endpoint returned no access_token")
        self._token = token
        self._token_expiry = time.time() + float(body.get("expires_in", 300)) - 30.0
        return token

    def _bearer(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        return self._fetch_token()

    # -- Requests with bounded retry --------------------------------------------

    def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1 + _RETRIES):
            try:
                headers = dict(kw.pop("headers", {}) or {})
                headers["Authorization"] = f"Bearer {self._bearer()}"
                resp = self._client.request(method, url, headers=headers, **kw)
            except OpenEMRAuthError:
                raise
            except (httpx.HTTPError, OpenEMRUnavailable) as exc:
                last_exc = exc
                log_event(
                    log, "openemr_api_error",
                    phase="request", error=type(exc).__name__, retries=attempt,
                )
                continue
            if resp.status_code == 401 and attempt < _RETRIES:
                self._token = None  # stale token — refresh once
                continue
            if resp.status_code >= 500 and attempt < _RETRIES:
                log_event(
                    log, "openemr_api_error",
                    phase="request", error=f"http_{resp.status_code}", retries=attempt,
                )
                continue
            return resp
        raise OpenEMRUnavailable(type(last_exc).__name__ if last_exc else "retries_exhausted")

    # -- API surface -----------------------------------------------------------

    def upload_document(
        self, pid: int, file_bytes: bytes, filename: str, mimetype: str, doc_type: str
    ) -> bool:
        """POST /api/patient/{pid}/document?path=... — returns only true/false
        (documents.id is recovered afterwards by sha3-512 hash SELECT)."""
        category = _CATEGORY_PATH.get(doc_type, "/Lab Report")
        resp = self._request(
            "POST",
            f"/apis/default/api/patient/{pid}/document",
            params={"path": category},
            files={"document": (filename or "document", file_bytes, mimetype)},
        )
        if resp.status_code in (401, 403):
            raise OpenEMRAuthError(f"document upload returned {resp.status_code}")
        if resp.status_code >= 400:
            log_event(log, "openemr_api_error", phase="store", error=f"http_{resp.status_code}")
            raise OpenEMRUnavailable(f"document upload returned {resp.status_code}")
        return True

    def download_document(self, pid: int, document_id: int) -> bytes:
        resp = self._request(
            "GET", f"/apis/default/api/patient/{pid}/document/{document_id}"
        )
        if resp.status_code in (401, 403):
            raise OpenEMRAuthError(f"document download returned {resp.status_code}")
        if resp.status_code >= 400:
            log_event(log, "openemr_api_error", phase="download", error=f"http_{resp.status_code}")
            raise OpenEMRUnavailable(f"document download returned {resp.status_code}")
        return resp.content


def document_storage_ready(settings: Settings | None = None) -> bool:
    """Cheap /ready probe (W2 rubric): is at least one path to persist a
    `documents` row usable — the OpenEMR REST front door configured, or the
    explicit DB fallback enabled? Config-coherence only; distinct from ping()
    (HTTP reachability) and the ingest_db SELECT 1. Never raises."""
    try:
        s = settings or get_settings()
        rest_configured = bool(s.openemr_base_url and s.openemr_oauth_client_id)
        return rest_configured or bool(s.ingest_db_doc_fallback)
    except Exception:  # noqa: BLE001 — a probe never raises
        return False


def ping(settings: Settings | None = None) -> bool:
    """Cheap /ready reachability probe: is the OpenEMR base URL answering?
    Never raises; unconfigured counts as not-ready (degraded, not down)."""
    s = settings or get_settings()
    if not s.openemr_base_url:
        return False
    try:
        resp = httpx.get(s.openemr_base_url, timeout=3.0, follow_redirects=True)
        return resp.status_code < 500
    except Exception as exc:  # noqa: BLE001
        log_event(log, "openemr_api_error", phase="ping", error=type(exc).__name__)
        return False
