"""Session-token mint + verify — the shared HMAC contract (AUTH_TOKEN_SPEC.md).

The Week-1 static bearer (a reusable secret injected into the browser) is
replaced by a short-lived token **minted by OpenEMR** after a real
`AclMain::aclCheckCore('patients','demo')` check and **verified here**.

Design invariants (from the spec — do not "simplify" these away):
- The signature covers the **literal `payload_b64url` string** carried in the
  token, never a re-serialized JSON. Verification decodes the segment only
  AFTER the HMAC check passes, so PHP<->Python JSON-canonicalization (key order,
  unicode, slash escaping) is irrelevant and can never cause a divergence.
- base64url, **no padding**. HMAC-SHA256. `key` = UTF-8 bytes of the secret
  string as-is (no hex decode). Signature compare is **constant time**.
- The token is a bearer credential: it MUST NEVER be logged (invariant #8).
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import time
from dataclasses import dataclass
from hashlib import sha256

log = logging.getLogger("copilot.session_token")

_VALID_SCOPES = frozenset({"patient", "day"})


class TokenInvalid(Exception):
    """Any verification failure — malformed, bad signature, expired, bad claims,
    or (fail-closed) an unavailable signing key. Maps to 401 at the HTTP edge."""


@dataclass(frozen=True)
class Claims:
    user_id: int  # OpenEMR users.id (the token's `sub`)
    usr: str      # OpenEMR username — audit only
    pid: int      # selected patient (0 for a day/ops token)
    scope: str    # "patient" | "day"
    acl: str      # the ACL verified at mint, e.g. "patients:demo"
    exp: int      # expiry, unix seconds


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _sign(payload_b64url: str, key: str) -> str:
    """HMAC-SHA256 over the ASCII bytes of the payload segment, base64url (no pad)."""
    mac = hmac.new(key.encode("utf-8"), payload_b64url.encode("ascii"), sha256).digest()
    return _b64url_encode(mac)


def mint(sub: int, usr: str, pid: int, scope: str, acl: str, key: str, ttl: int, now: int) -> str:
    """Build a token. Canonical (sorted-key, compact) JSON so PHP + Python mint
    byte-identical payloads — but verification never depends on that (it works
    over whatever literal segment it is handed)."""
    payload = {
        "v": 1,
        "sub": int(sub),
        "usr": usr,
        "pid": int(pid),
        "scope": scope,
        "acl": acl,
        "iat": int(now),
        "exp": int(now) + int(ttl),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64url = _b64url_encode(payload_json.encode("utf-8"))
    return payload_b64url + "." + _sign(payload_b64url, key)


def verify(token: str, key: str, now: int, skew: int = 60) -> Claims:
    """Verify a token per the spec's algorithm. Raise `TokenInvalid` on ANY
    failure (fail-closed). Does NOT re-serialize the JSON payload."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise TokenInvalid("malformed")
    payload_b64url, sig_b64url = token.split(".")
    if not payload_b64url or not sig_b64url:
        raise TokenInvalid("malformed")

    # 1) Verify over the LITERAL segment, constant time, BEFORE decoding.
    expected = _sign(payload_b64url, key)
    if not hmac.compare_digest(expected, sig_b64url):
        raise TokenInvalid("bad_signature")

    # 2) Only now decode + parse the (authenticated) payload.
    try:
        payload = json.loads(_b64url_decode(payload_b64url))
    except Exception as exc:  # noqa: BLE001
        raise TokenInvalid("undecodable_payload") from exc
    if not isinstance(payload, dict):
        raise TokenInvalid("payload_not_object")
    if payload.get("v") != 1:
        raise TokenInvalid("bad_version")

    try:
        sub = int(payload["sub"])
        usr = str(payload["usr"])
        pid = int(payload["pid"])
        scope = str(payload["scope"])
        acl = str(payload["acl"])
        iat = int(payload["iat"])
        exp = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenInvalid("missing_or_bad_claims") from exc

    if scope not in _VALID_SCOPES:
        raise TokenInvalid("bad_scope")
    if exp < now - skew:
        raise TokenInvalid("expired")
    if iat > now + skew:
        raise TokenInvalid("future_iat")

    return Claims(user_id=sub, usr=usr, pid=pid, scope=scope, acl=acl, exp=exp)


# --- signing key (DB-backed, short-TTL cache) ---------------------------------

_KEY_TTL_S = 60.0
_key_cache: dict[str, object] = {"value": None, "ts": 0.0}


def _reset_key_cache() -> None:
    """Test hook: drop the cached signing key."""
    _key_cache["value"] = None
    _key_cache["ts"] = 0.0


def get_signing_key() -> str:
    """Read the HMAC secret from `copilot_signing_key` (id=1) via the read-only
    connection, cached ~60s so a rotation propagates. In dev mode, if the table
    is absent, fall back to a fixed dev key (logged) so the local UI + load tests
    work without the seeder. In production a missing key fails closed."""
    now = time.monotonic()
    cached = _key_cache["value"]
    if isinstance(cached, str) and (now - float(_key_cache["ts"])) < _KEY_TTL_S:
        return cached

    from . import db
    from .config import get_settings

    key: str | None = None
    try:
        rows = db.query("SELECT secret FROM copilot_signing_key WHERE id = 1")
        if rows and rows[0].get("secret"):
            key = str(rows[0]["secret"])
    except Exception as exc:  # noqa: BLE001 - table absent or DB unreachable
        # PHI-safe: log the failure TYPE only, never the query or any value.
        log.warning("signing_key_read_failed error=%s", type(exc).__name__)

    if not key:
        s = get_settings()
        if s.auth_dev_mode:
            log.warning("signing_key_table_absent using_dev_fallback=true")
            key = s.dev_signing_key
        else:
            # Fail closed: no key -> no token can be trusted.
            raise TokenInvalid("signing_key_unavailable")

    _key_cache["value"] = key
    _key_cache["ts"] = now
    return key
