# Co-Pilot Session Token — Shared Contract (v1)

The Week-1 static bearer (a reusable secret string-injected into the browser) is
**replaced** by a short-lived token **minted by OpenEMR** after a real
`AclMain::aclCheckCore('patients','demo')` check, and **verified by the Python
sidecar**. This file is the single source of truth both sides implement against.

## Why
Grader feedback: the reusable browser bearer must become "a short lived server
side token tied to the OpenEMR session and selected patient," and authorization
must "enforce the actual OpenEMR ACL instead of treating an existing user and
provider match as the full authorization decision." OpenEMR (which owns the
session + gacl) mints; the sidecar enforces by requiring a valid, unexpired,
patient-bound token. Provider-match remains only as the per-patient **scope**
layer on top of the real ACL — not the whole decision.

## Token format
```
token = payload_b64url + "." + sig_b64url
```
- `payload_b64url` = base64url(**no padding**) of the UTF-8 JSON payload.
- `sig_b64url`     = base64url(**no padding**) of `HMAC_SHA256(key, payload_b64url_ascii_bytes)`.
- **The signature covers the literal `payload_b64url` string** (the exact bytes
  in the token), NOT a re-serialized payload. **The verifier MUST NOT
  re-serialize the JSON** — it verifies over the segment it received, then
  decodes. This removes all PHP↔Python JSON-canonicalization fragility (key
  order / unicode / slash escaping are irrelevant).

### HMAC key
- `key` = the **UTF-8 bytes of the secret string** stored in the DB (below). Both
  sides use the string as-is (no hex-decoding).

### Payload claims (JSON object)
| field | type | meaning |
|---|---|---|
| `v`     | int    | format version, always `1` |
| `sub`   | int    | OpenEMR `users.id` of the logged-in user (from `$_SESSION['authUserID']`) |
| `usr`   | string | OpenEMR username (`$_SESSION['authUser']`) — audit only |
| `pid`   | int    | the selected patient (`0` for a day/ops-scoped token) |
| `scope` | string | `"patient"` \| `"day"` |
| `acl`   | string | the ACL that was verified at mint, e.g. `"patients:demo"` |
| `iat`   | int    | issued-at, unix seconds |
| `exp`   | int    | expiry, unix seconds (= iat + TTL) |

## Verification algorithm (sidecar)
1. Split on the single `.`; exactly two non-empty segments, else reject.
2. Recompute `HMAC_SHA256(key, payload_b64url)` and compare to `sig_b64url` in
   **constant time**. Mismatch → reject (401).
3. base64url-decode `payload_b64url`, parse JSON. Malformed → reject (401).
4. `v == 1`, required fields present + correct types, else reject.
5. `exp >= now - SKEW` (SKEW = 60s). Expired → reject (401).
   Optionally also `iat <= now + SKEW`.
6. Return claims `{user_id: sub, usr, pid, scope, acl}`.

### Endpoint binding (after verification)
- **Patient endpoints** (`/chat`, `/snapshot`, `/ingest`, `/documents/*`):
  require `scope == "patient"` AND `claims.pid == the request's pid`
  (`body.pid` / `?pid` / path). Mismatch → **403** (a token for patient A can
  never act on patient B).
- **Day endpoint** (`/day`): require `scope == "day"`.
- Then run the existing `authorize(user_id = claims.user_id, pid)` **per-patient
  scope** check (provider match). The **ACL** was already enforced by OpenEMR at
  mint (a token only exists if `aclCheckCore` passed); the sidecar additionally
  asserts `claims.acl == "patients:demo"`. Both layers must pass.
- `user_id` now comes from the **token**, never from `settings.demo_user_id`.

## TTL
- `TTL = COPILOT_TOKEN_TTL_S` (default **900** = 15 min). `exp = iat + TTL`.

## Signing key — stored in the DB (openemr database)
Both OpenEMR PHP (native access) and the sidecar (`copilot_ro` already has
`SELECT ON openemr.*`) read the key from the **openemr** database — no cross-DB
grant, and **no env-var change on the openemr service** (the runbook's DB-wipe
risk is avoided).
```sql
CREATE TABLE IF NOT EXISTS copilot_signing_key (
  id         INT NOT NULL PRIMARY KEY,
  secret     VARCHAR(128) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
INSERT IGNORE INTO copilot_signing_key (id, secret) VALUES (1, '__SIGNING_KEY__');
```
- The seeder's `run.sh` generates `SIGNING_KEY=$(openssl rand -hex 32)` and
  substitutes `__SIGNING_KEY__`. `INSERT IGNORE` keeps the key **stable across
  reseeds** (only ever set once).
- Sidecar reads `SELECT secret FROM copilot_signing_key WHERE id = 1` via the
  read-only connection and caches it (~60s TTL) so a future rotation propagates.
- OpenEMR PHP reads it with `sqlQuery("SELECT secret FROM copilot_signing_key WHERE id = 1")`.

## Where the token reaches the browser
- OpenEMR builds the iframe src with the token in the **URL fragment** (not the
  query string — fragments are never sent to the server, so the token stays out
  of sidecar access logs):
  `{sidecar}/?pid=X&embed=overlay#t=<token>` (overlay), and
  `{sidecar}/?view=day&embed=emr#t=<token>` (My Day, a `scope:"day"` token).
- The panel JS reads the token from `location.hash`, uses it as the `Bearer`
  value, and **the sidecar no longer injects any `__COPILOT_TOKEN__`** into HTML.
- My Day mints a `scope:"day"` token; clicking a patient opens that patient's
  chart, whose overlay mints a fresh `scope:"patient"` token for that pid.

## Revoke / remove the static bearer
- Remove `require_bearer` (static) and every `__COPILOT_TOKEN__` HTML injection.
- Rotate `COPILOT_BEARER_SECRET` on production to an inert value (the value was
  exposed; it must no longer grant access). Patient access requires an
  OpenEMR-minted token exclusively.
- Ops endpoints (`/metrics/summary`, `/dashboard`) accept any valid session
  token; headless/CI use a server-side mint. No reusable credential is ever
  placed in a patient-facing browser context.

## Dev mode (local only)
`COPILOT_AUTH_DEV_MODE=true` (default **false**; NEVER set in production) lets the
sidecar mint a `scope:"patient"` token for `demo_user_id`/`default_pid` so the UI
and load tests work without a running OpenEMR. Production leaves it false and
requires real OpenEMR-minted tokens.

## Golden interop vector (both sides MUST test against this)
```
KEY            = golden-test-key-do-not-use-in-prod
PAYLOAD (json) = {"acl":"patients:demo","exp":1752000900,"iat":1752000000,"pid":101,"scope":"patient","sub":42,"usr":"drchen","v":1}
PAYLOAD_B64URL = eyJhY2wiOiJwYXRpZW50czpkZW1vIiwiZXhwIjoxNzUyMDAwOTAwLCJpYXQiOjE3NTIwMDAwMDAsInBpZCI6MTAxLCJzY29wZSI6InBhdGllbnQiLCJzdWIiOjQyLCJ1c3IiOiJkcmNoZW4iLCJ2IjoxfQ
SIG_B64URL     = WCeKuHjl7PtagFHKyHJjQmxn8EigC9nvjytqSFl_h8A
TOKEN          = eyJhY2wiOiJwYXRpZW50czpkZW1vIiwiZXhwIjoxNzUyMDAwOTAwLCJpYXQiOjE3NTIwMDAwMDAsInBpZCI6MTAxLCJzY29wZSI6InBhdGllbnQiLCJzdWIiOjQyLCJ1c3IiOiJkcmNoZW4iLCJ2IjoxfQ.WCeKuHjl7PtagFHKyHJjQmxn8EigC9nvjytqSFl_h8A
```
- Python & PHP verifiers MUST accept `TOKEN` under `KEY` and extract the claims.
- Flipping any character of `TOKEN` MUST reject.
- A token whose `exp` < now-60 MUST reject.
- Verification MUST NOT depend on re-serializing the JSON (verify over the
  literal `PAYLOAD_B64URL`).

## Denial test (grader requirement)
A **valid user who lacks `patients|demo`** must be denied. Because OpenEMR mints,
the denial is authoritative at mint: `aclCheckCore('patients','demo')` returns
false → **no token is issued** → `myday.php` shows an explicit "not available for
your role" page and the chart overlay renders nothing.

Two tiers of coverage:

1. **Sidecar hermetic tests** (`copilot/tests/test_session_auth.py`, in CI): no
   token → 401; expired/tampered token → 401; a token for patient A used on
   patient B → 403; a valid token whose user is not that patient's provider →
   403 (the per-patient scope layer). The golden interop vector is asserted here.

2. **ACL role-denial test** (`bin/copilot_auth_check.php`, run in the OpenEMR
   container as the web user): `php copilot_auth_check.php <username>` runs the
   real `AclMain::aclCheckCore('patients','demo', <username>)`. A user WITH the
   role → `ALLOW` (exit 0); a user WITHOUT it → `DENY` (exit 3). Verified: `admin`
   → ALLOW/0.

**Creating the role-lacking fixture user** (the phpGACL id sequences are managed
by OpenEMR's own API, so create the user through Admin, not raw SQL — 30s):
Administration → Users → Add User; set a username/password; set **ACL Group =
"Accounting"** (or "Front Office") — a role that does not grant `patients|demo`;
Save. Then:
`su -s /bin/sh apache -c "php .../oe-module-copilot/bin/copilot_auth_check.php <username>"`
→ prints `DENY`, exits 3. Logging in as that user and opening the Co-Pilot menu
shows the "not available for your role" page. (A Physician/Clinician user → ALLOW,
token minted, panel works.)

## As-built (verified against the shipped tree)

Every clause above is implemented and testable:

| Contract clause | Shipped in |
| --- | --- |
| PHP mint (`CopilotToken::mint` / `signingKey`) | `copilot/deploy/openemr-image/module/src/CopilotToken.php` |
| ACL gate + fragment token (chart overlay) | `.../module/src/Bootstrap.php` (`aclCheckCore('patients','demo')` → `?pid=X&embed=overlay#t=`) |
| ACL gate + `scope:"day"` token (My Day) | `.../module/public/myday.php` (403 "not available for your role" page on deny) |
| Denial + token self-test CLI | `.../module/bin/copilot_auth_check.php` (`ALLOW`/0, `DENY`/3, golden-vector self-test) |
| Sidecar verify + fail-closed key | `copilot/app/session_token.py` (`verify`, `get_signing_key`, `TokenInvalid`) |
| Endpoint binding (401 verify / 403 patient-mismatch / `scope` checks) | `copilot/app/main.py` (`require_session_token`, `enforce_patient`; `/day` requires `scope=="day"`; `/metrics/summary` accepts any valid token) |
| Dev-only mint (404 unless dev mode) | `copilot/app/main.py::dev_token` (`GET /dev/token`) |
| Signing-key table + `INSERT IGNORE` + `ALTER USER` idempotency | `copilot/deploy/seeder/live_seed.sql`; key generation in `copilot/deploy/seeder/run.sh` |
| Hermetic auth tests (401/403/expired/tampered + golden vector) | `copilot/tests/test_session_auth.py` |
