"""Authorization Gateway — the per-patient SCOPE layer.

Two-layer authorization (matches CLAUDE.md invariant #2, AUTH_TOKEN_SPEC.md):

1. **The ACL/role decision is delegated to OpenEMR gacl.** OpenEMR runs
   `AclMain::aclCheckCore('patients','demo')` at token-mint time; a session
   token only exists if that passed. The sidecar therefore does NOT re-implement
   the gacl engine — it trusts the token's existence, and additionally asserts
   the `acl` claim (`"patients:demo"`) at the HTTP edge (see main.enforce_patient).
2. **This function composes the per-patient scope on top** — the layer gacl
   leaves to the caller (gacl is section/category-based, allow-all at patient
   grain). The requesting user must be the patient's assigned provider
   (`patient_data.providerID`). Fail closed on NULL / mismatch / missing patient.

The identity (`user_id`) is the token's `sub` (OpenEMR `users.id`) — NEVER taken
from the client, never a server pin in the token era. Every decision is
audit-logged (user / pid / when / correlation-id) for HIPAA access-tracking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .correlation import log_event
from .db import query

log = logging.getLogger("copilot.authz")


@dataclass(frozen=True)
class AuthzDecision:
    allowed: bool
    user_id: int
    pid: int
    reason: str


def _user_exists(user_id: int) -> bool:
    return bool(query("SELECT id FROM users WHERE id = :uid LIMIT 1", {"uid": user_id}))


def _patient_provider(pid: int) -> tuple[bool, int | None]:
    rows = query(
        "SELECT pid, providerID FROM patient_data WHERE pid = :pid LIMIT 1",
        {"pid": pid},
    )
    if not rows:
        return (False, None)
    return (True, rows[0]["providerID"])


def _deny(user_id: int, pid: int, reason: str) -> AuthzDecision:
    log_event(log, "authz_deny", user_id=user_id, pid=pid, reason=reason)
    return AuthzDecision(False, user_id, pid, reason)


def authorize(user_id: int, pid: int) -> AuthzDecision:
    """Deterministic user↔patient check. Called BEFORE any read."""
    if not _user_exists(user_id):
        return _deny(user_id, pid, "unknown_user")
    exists, provider_id = _patient_provider(pid)
    if not exists:
        return _deny(user_id, pid, "patient_not_found")
    if provider_id is None or int(provider_id) != int(user_id):
        # Fail closed: gacl is section-based (allow-all at patient grain), so the
        # provider match is the only real per-patient signal — no permissive
        # fallback.
        return _deny(user_id, pid, "not_patient_provider")
    log_event(log, "authz_allow", user_id=user_id, pid=pid)
    return AuthzDecision(True, user_id, pid, "authorized")
