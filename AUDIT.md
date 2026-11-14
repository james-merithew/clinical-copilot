# AUDIT.md — Clinical Co-Pilot (OpenEMR fork)

**Scope.** Audit of the `openemr-base-clean` fork before building the AI layer, across five passes: Security, Performance, Architecture, Data Quality, and Compliance & Regulatory.

**Methodology & honesty note.** This audit is **grounded in the fork's codebase and OpenEMR's documented, well-known characteristics** (directory layout, auth model, schema, API surface). Quantitative findings — exact slow queries, row counts, and data-quality prevalence — should be **confirmed on the running fork with the loaded demo data** before Early Submission; a "confirm live" checklist is at the end. Only demo data is used, per the case study.

---

## Summary (~500 words) — the findings that shaped the architecture

OpenEMR is a large, real, legacy-heavy PHP application backed by MariaDB, with a modern REST/FHIR API layer bolted onto an older monolith. Five findings drove our AI-integration plan.

**1. Authorization is real but must be delegated, never re-implemented (Security/Architecture).** OpenEMR ships a mature access-control system (`gacl`-based ACLs: users → roles → sections/return-values) plus session auth for the UI and OAuth2/JWT for the API (`oauth2/`, `_rest_routes.inc.php`, FHIR). The most important architectural consequence: our agent must **delegate authorization to this existing ACL on every read** and never invent its own permission logic. This is why our design puts a deterministic Authorization Gateway in front of every tool call and treats the LLM as having no authority.

**2. Data lives in multiple places and isn't always clean (Data Quality).** The single most agent-relevant finding: **medications exist in two tables** — `prescriptions` *and* `lists` (`type='medication'`) — and can diverge; an `active` flag can be stale. Problems/allergies live in `lists`; labs in `procedure_order/report/result` as strings with inconsistent units/ranges and sometimes-missing reference ranges; encounter reasons are free-text. Missing fields, duplicates, and stale rows are the norm, not the exception. **These are agent failure modes**, and they justify our hard-source-attribution invariant (bind every claim to an authoritative primary key) and our "surface the gap, never infer" degradation rule.

**3. Latency is bounded by PHP + per-query DB round-trips (Performance).** The patient-summary experience aggregates from many tables, and OpenEMR does no result caching by default; PHP request overhead and N+1-style access on some screens set a real ceiling. This directly shaped our **two-phase response** (prefetch on chart-open, parallel tool fan-out, stream first content ≈1.5s, defer deep history) and our choice of **direct read-only DB reads** (with the FHIR API as the auth-respecting alternative) to stay inside the 90-second budget.

**4. PHI exposure vectors are everywhere legacy code touches logs and transport (Security/Compliance).** Debug/error paths can leak PHI into logs; demo deployments frequently run without enforced TLS and with default credentials. Our AI layer adds two new vectors — **PHI sent to an LLM** (mitigated by BAA + zero-data-retention) and **prompt injection via free-text notes** (mitigated structurally: the model holds no DB handle and no authz, so an injected instruction still can't reach unauthorized data). Our logs carry correlation IDs and source keys, never PHI values.

**5. Sensitive-data segmentation is a gap (Compliance).** OpenEMR does not, by default, segment specially protected records (e.g., 42 CFR Part 2 substance-use, behavioral health) from ordinary patient access. Our gateway checks user↔patient but not protected-category — a known limitation we park as a hardening item rather than hide.

Net: the audit turned "build an agent" into "build a **thin, read-only, deterministically-authorized, source-attributed** integration beside OpenEMR" — which is exactly the architecture we defend.

---

## 1. Security audit

- **Authentication.** UI uses PHP session auth; the API uses **OAuth2 + JWT** (`oauth2/`). Demo installs ship with default credentials that must be rotated before any public deployment — a real risk given the Stage-2 public-URL gate.
- **Authorization.** `gacl`-based ACLs (`gacl/`, `acl_upgrade.php`): granular but complex, and misconfiguration is easy. This is the surface our Authorization Gateway delegates to; we never re-implement it.
- **Injection surface.** Legacy PHP with a large historical CVE record for SQLi/XSS/CSRF. Newer code (`src/`, service classes, `QueryUtils`) uses parameterized queries; older `interface/` code sometimes concatenates. **Our mitigation:** a dedicated **read-only DB user**, parameterized/patient-scoped queries only, and no write path.
- **PHI in logs / transit.** Error and debug output can leak PHI; TLS is often unenforced in demo. Enforce HTTPS; keep PHI out of application logs.
- **New AI-layer vectors.** (a) PHI to LLM → **BAA + ZDR**; (b) **prompt injection** via EHR free-text → record text is passed as data, structurally separated, and the model has no privileged handle, so injection yields only authorized, patient-scoped tool requests.
- **Secrets.** DB credentials in `sites/*/sqlconf.php`, API keys — keep in Railway private env/secrets, never in code or logs.

## 2. Performance audit

- **Backend.** MariaDB; no query-result caching by default. PHP per-request overhead (Apache/mod_php or php-fpm).
- **Hot tables.** `form_encounter`, `forms`, `lists`, `prescriptions`, `procedure_result`, `billing`, and the audit `log` (which grows large and adds write latency).
- **Access patterns.** The patient summary aggregates across many tables; some screens exhibit N+1 access. Indexing on `pid` is generally present, but multi-table joins for a full picture are not free.
- **Impact on the agent.** Each tool read is a DB round-trip; this is the concrete reason for **prefetch + parallel fan-out + streaming** and for deferring deep history. It also motivates a future **read replica** at scale.
- **Confirm live:** enable the slow-query log, capture p50/p95 for the summary and per-tool queries under the demo data.

## 3. Architecture audit

- **Shape.** PHP monolith. Key directories: `interface/` (UI pages — where the chart and our embedded panel live), `library/` (legacy helpers), `src/` (modern PSR-4 `OpenEMR\…` services), `sql/` + `db/` (schema/migrations), `oauth2/` + `_rest_routes.inc.php` + FHIR (`FHIR_README.md`, `swagger/`), `gacl/` (ACL), and `docker/` + Docker Compose.
- **Data layer.** MariaDB via legacy `sqlStatement()` and newer service classes / `QueryUtils`.
- **Integration points for the agent.** (a) **UI** — embed the Co-Pilot panel in `interface/` on the patient summary; (b) **data** — read-only MariaDB user *or* the OAuth2 **FHIR API**; (c) **authz** — the `gacl` ACL; (d) **identity** — OpenEMR session / OAuth token.
- **Consequence.** The clean, low-risk integration is a **separate sidecar + an embedded panel**, not modifying core PHP — "integrate cleanly rather than bolt something on."

## 4. Data Quality audit

- **Medications in two tables** — `prescriptions` and `lists` (`type='medication'`); values can diverge and `active` can be stale. The tool layer must pick the authoritative row and attribute to its PK.
- **Problems/allergies** — `lists` (`type='medical_problem'` / `'allergy'`); free-text titles, inconsistent/absent coding.
- **Labs** — `procedure_result` with string values, inconsistent units, and sometimes-missing reference ranges (affects abnormal-flag logic).
- **Encounters** — `form_encounter`; free-text `reason`; some encounters lack linked forms.
- **Demo data** — synthetic and sparse; empty fields and possible duplicate patients/stale meds.
- **Consequence.** Directly motivates **hard source attribution**, the deterministic **constraint engine** (allergy/interaction/threshold from structured data), and the **"surface the gap, never infer"** failure rule.
- **Confirm live:** count meds present in `prescriptions` but not `lists` (and vice-versa), labs missing reference ranges, and encounters missing forms.

## 5. Compliance & Regulatory audit

- **Audit logging.** OpenEMR has an audit `log` table (and optional stronger audit). Ensure **access logging** (who viewed which patient); our gateway adds a **correlation-ID** audit trail for every agent access.
- **Minimum necessary.** Our **patient-scoped, column-scoped, read-only** reads support HIPAA minimum-necessary by construction.
- **Data retention & breach.** Confirm PHI retention configuration; document breach-notification obligations if PHI is exposed.
- **BAA implications of the LLM.** Sending PHI to an LLM provider requires a **BAA**; per the case study we assume signed BAAs with **no training use (ZDR)**.
- **Sensitive-data segmentation (gap).** No default segmentation for **42 CFR Part 2** / behavioral-health records; our gateway checks user↔patient but not protected-category. Parked as a hardening item, named openly.
- **Demo data only**, per the case study.

---

## Confirm-live checklist (before Early Submission)
- [x] Rotate default credentials; enforce TLS on the public OpenEMR service.
- [~] Capture slow-query log + p50/p95 for the summary and per-tool queries. — **p50/p95 captured as floor values; the slow-query log itself is verified-as-design / unmeasured on sparse demo data** (nothing exceeds `long_query_time`).
- [~] Quantify med divergence (`prescriptions` vs `lists`), labs missing ranges, encounters missing forms. — **med divergence + encounters-missing-forms measured; labs-missing-ranges is verified-as-design / unmeasured on sparse demo data** (`procedure_result` has 0 rows).
- [x] Confirm the exact `gacl` check surface for user↔patient resolution (API vs. session context).
- [x] Verify PHI is absent from application logs; confirm the audit `log` records patient access.

### Confirm-live results (2026-07-07)

Run against the running fork with `DEMO_MODE=standard` demo data. Data-quality,
timing, and log checks were run on the **local dev stack**
(`docker/development-easy-light`, identical demo dataset); credential/TLS checks
against the **live Railway service**
(`https://openemr-production-31ad.up.railway.app`). Demo data is deliberately
sparse, so several counts are floors, not production-representative — called out
below.

**1. Credentials + TLS.** Live OpenEMR admin password rotated off the `admin/pass`
default to a strong generated value; DB credentials live only in Railway env
vars (never in the repo). TLS enforced at Railway's edge: plain `http://` →
`301` redirect to `https://` with a valid edge certificate. (The local dev stack
intentionally keeps `admin/pass`.)

**2. Query latency (patient-summary fan-out, pid=1, `SHOW PROFILES`).** Six
representative per-tool reads totalled **~13.6 ms of DB execution**:

| Query | Duration |
| --- | --- |
| problems (`lists` medical_problem) | 0.45 ms |
| meds (`lists` medication) | 0.24 ms |
| meds (`prescriptions`) | 0.17 ms |
| allergies (`lists` allergy) | 0.26 ms |
| encounters (`form_encounter` ORDER BY date) | **10.5 ms** |
| coded dx (`billing`) | 1.98 ms |

Latency is **round-trip/PHP-overhead-bound, not per-query-CPU-bound** — validating
the two-phase prefetch + parallel fan-out design. At demo-data size the slow-query
log (default `long_query_time=10s`) captures nothing; these are floor values. The
`form_encounter` sort is the relative hot spot to watch as data grows.

**3. Data-quality quantification.**
- **Medication divergence — confirmed.** `lists` (`type='medication'`) holds **5**
  rows vs **1** in `prescriptions`; **3** medications appear in `lists` with **no**
  matching `prescriptions` row (0 the other way); **4** `lists` meds are flagged
  active. The two tables already disagree on this tiny dataset — exactly the
  divergence that motivates hard source attribution.
- **Labs missing reference ranges — not exercisable on demo data.** `procedure_result`
  has **0** rows in the standard demo set, so range-completeness can't be measured
  here; the finding stands from schema/documentation and should be re-confirmed on
  richer data.
- **Encounters missing forms — none in demo.** **3** encounters, **0** without a
  linked `forms` row.

**4. gacl check surface.** Central check is `AclMain::aclCheckCore($section, $value,
$user, $return_value)` (`src/Common/Acl/AclMain.php:166`), which wraps the gacl
engine `acl_check()` (`src/Gacl/Gacl.php:229`), with `aclCheckForm` / `aclCheckIssue`
wrappers. The same ACL gates both the session UI and the REST/FHIR API (after OAuth2
scope validation). **Granularity nuance:** OpenEMR authorization is section/category-
based (e.g. "may this user access patient records"), **not per-patient ACL by
default** — patient identity is resolved by `pid` in session/URL. Our gateway
delegates to `aclCheckCore` and adds the user↔patient scoping the core doesn't
enforce per-row.

**5. PHI in logs + audit trail.** Spot check of the OpenEMR container `/var/log`
for demo patient surnames (Belford/Underwood/Moore) returned **no matches** — PHI
not leaking into application logs on this run (re-verify once chart browsing is
exercised). The audit `log` table (**2,116** rows) records `user`, `patient_id`,
`event`, and `success` per action (e.g. `patient-record-update`, user `admin`,
`patient_id=3`) — **access logging with user↔patient binding is present**.
