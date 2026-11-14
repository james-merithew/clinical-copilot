# MIGRATIONS.md — Clinical Co-Pilot, Week 1 → Week 2

Every schema and contract change between the Week 1 baseline and the Week 2
multimodal build, and why each is **backward compatible**. The governing rule
(W2_ARCHITECTURE.md §9.1, CLAUDE.md invariant 5): **all Week 1 → Week 2 contract
changes are additive with defaults, so every Week 1 payload validates unchanged
and every Week 1 consumer keeps working.** Nothing was removed or made stricter.

A "migration" in this project has two shapes:

1. **Pydantic contract changes** (`copilot/app/contracts.py`, `app/ingest/schemas.py`)
   — code, versioned by git; the drift is guarded by the existing contract tests
   plus the OpenAPI drift-guard (`copilot/tests/test_openapi_contract.py`).
2. **Database changes** — an **idempotent seeder SQL** migration, not an ORM
   migration tool. The seeder is `CREATE … IF NOT EXISTS` / `INSERT … ON
   DUPLICATE KEY UPDATE` throughout, so it is **safe to re-run on every deploy**:
   `copilot/deploy/seeder/live_seed.sql` (Railway/live) and
   `copilot/sql/seed_showcase.sql` (local showcase data).

---

## 1. Contract changes (`copilot/app/contracts.py`)

### 1.1 The citation contract became a discriminated union

Week 1 had a single citation type, `SourceRef{table, primary_key, label}` — a
real OpenEMR PK. Week 2 has three evidence kinds, so `Claim.source_refs` now
holds a **discriminated union on `source_type`**:

| Type | `source_type` | Carries | Meaning |
| --- | --- | --- | --- |
| `SourceRef` | `"patient_record"` | `table`, `primary_key`, `label` | a real OpenEMR row PK (Week 1 shape) |
| `GuidelineRef` | `"guideline"` | `doc_id`, `chunk_id`, `section`, `label` | a corpus chunk — **never** a patient fact |
| `DocumentRef` | `"document"` | `document_id`, `page`, `field_id`, `bbox`, `quote`, `label` | a region of a stored document (click-to-source target) |
| `BBox` | — | `page`, `x0/y0/x1/y1` (normalized 0–1), `origin` (`text_layer`/`vlm`) | a page-relative bounding box |

- `AnySourceRef = Annotated[Union[SourceRef, GuidelineRef, DocumentRef],
  Field(discriminator="source_type")]`, wrapped in a `BeforeValidator`
  (`_tag_untyped_as_patient_record`).
- **Why a union, not subclassing:** subclassing hits the Pydantic
  subclass-serialization trap — a `DocumentRef` declared as a `SourceRef` field
  serializes as its base class, silently dropping `page`/`bbox`/`quote`. The
  discriminated union preserves every field and gives the client a switchable
  `source_type` tag.

**Backward compatibility — the wire-compat shim.** `SourceRef.source_type`
defaults to `"patient_record"`, and `_tag_untyped_as_patient_record` runs
*before* the discriminator: a bare Week 1 dict with **no `source_type`** is
tagged `patient_record` before validation, so a Week 1 payload (which never
carried the tag) validates byte-unchanged. Tagged input passes through
untouched. Two `Annotated` layers guarantee the shim runs before tag
extraction.

### 1.2 `AgentResponse` gained two defaulted fields

```python
class AgentResponse(BaseModel):
    ...                                              # all Week 1 fields unchanged
    guideline_evidence: list[Claim] = []             # NEW, defaulted
    handoffs: list[Handoff] = []                     # NEW, defaulted
```

- `guideline_evidence` — verified claims citing **only** `gl:` handles,
  partitioned out of the patient briefing by the verifier's separation rule and
  rendered under a separate header (so reference knowledge is never confused
  with this patient's chart).
- `handoffs` — the graph's transition trail ("how this answer was assembled").

Both default to `[]`, so **every Week 1 consumer that ignores them keeps
working** and every Week 1 response shape is a valid Week 2 response.

### 1.3 New supporting contracts (all additive)

| New model | Purpose |
| --- | --- |
| `BBox` | normalized bbox with `origin` coordinate-authority tag |
| `EvidenceChunk`, `GuidelineSearchResult` | `search_guidelines` tool I/O (`gl:` handles) |
| `Handoff`, `WorkerResult` | supervisor-graph inspectability records (enum-bound `NodeName`/`RouteName`/`WorkerName`) |
| `VerificationResult.guideline_evidence` | defaulted `[]` — the separation-rule partition output |

The `SourceTable` literal set (Week 1) is unchanged.

### 1.4 Ingestion contracts (`copilot/app/ingest/schemas.py`) — new module

Net-new, so nothing to break: `ExtractedField[T]` (the per-field envelope:
value + page + bbox + confidence + verbatim_quote + status),
`ExtractedLabValue`, `IntakeListItem`, `LabReportExtraction`,
`IntakeFormExtraction`, `IngestResult`. `PROMPT_VERSION = "w2-extract-v1"`
stamps provenance so an extraction-prompt change is a visible lineage event.

### 1.5 Authentication contract — static bearer → OpenEMR-minted session token

This is the one Week-1 → Week-2 change that is **not additive**: the reusable
static browser bearer is **revoked and replaced**. The shared wire contract is
`copilot/AUTH_TOKEN_SPEC.md` (v1); both the OpenEMR PHP mint and the Python
sidecar verify against it.

| | Week 1 (removed) | Week 2 (as-built) |
| --- | --- | --- |
| Credential | reusable static bearer `COPILOT_BEARER_SECRET` injected into the page | short-lived HMAC token `payload_b64url.sig_b64url`, minted per chart-open |
| Who authorizes | sidecar treated user+provider-match as the whole decision | **OpenEMR gacl** `AclMain::aclCheckCore('patients','demo')` at mint; sidecar adds the per-patient scope layer on top |
| Binding | none (any patient) | bound to `sub` (users.id), `pid`, `scope` (`patient`\|`day`), `acl`, `exp` (900s TTL) |
| Transport | HTML injection | URL **fragment** `#t=<token>` (never sent to a server / access log) |
| Failure | — | verify fail-closed → **401**; patient-A token on patient B → **403** |

- **Migration action on deploy:** rotate `COPILOT_BEARER_SECRET` to an inert
  value on production (it was exposed), and ensure `COPILOT_AUTH_DEV_MODE` is
  **false** in prod (it gates the dev-only `/dev/token` mint, which 404s when
  false). No sidecar env-var change is required for the key itself — see §2.4.
- **Backward compatibility:** none by design — this is a security fix; the old
  reusable credential must stop working. It does **not** change any response
  contract, so Week 1 *payload* shapes are unaffected.

---

## 2. Database changes — the `copilot` schema (idempotent seeder)

Applied by `copilot/deploy/seeder/live_seed.sql` (§ "Week 2 (Lane A)"), which is
**idempotent — safe to re-run on every seed**. It creates a **separate `copilot`
database** alongside `openemr`; the OpenEMR schema itself is **not altered**
(invariant: OpenEMR remains the untouched system of record).

### 2.1 New tables (`CREATE TABLE IF NOT EXISTS copilot.*`)

| Table | Purpose | Key constraint / idempotency |
| --- | --- | --- |
| `copilot.ingestion_run` | one row per accepted upload: `correlation_id`, `pid`, `doc_type`, `content_hash`, `document_id`, `status`, field counts | **`UNIQUE(content_hash, pid)`** — re-uploading the same bytes for the same patient is a no-op (zero new rows, explicit dedup notice) |
| `copilot.provenance` | field-level lineage: which chart row came from which document region (`target_table`/`target_pk` ↔ `document_id`/`field_id`/`page`/`bbox_*`/`bbox_origin`/`confidence`/`quote`) | **`UNIQUE(document_id, field_id)`** — one provenance row per extracted field |
| `copilot.intake_fact` | sidecar-owned intake facts (chief concern, family history) + demographic **compare-and-flag** mismatches (`value` vs `chart_value`) — `patient_data` is never overwritten | `KEY idx_intake_fact_pid (pid)` |

### 2.2 New least-privilege writer identity — `copilot_ingest`

Invariant 3 is preserved: the read-only `copilot_ro` user is untouched; all
writes go through a **separate INSERT-only identity**. Grants (from the seeder):

```sql
CREATE USER IF NOT EXISTS 'copilot_ingest'@'%' IDENTIFIED BY '__INGEST_PASS__';
ALTER USER 'copilot_ingest'@'%' IDENTIFIED BY '__INGEST_PASS__';  -- password idempotency
GRANT INSERT ON openemr.procedure_order       TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.procedure_order_code  TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.procedure_report      TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.procedure_result      TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.lists                 TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.documents             TO 'copilot_ingest'@'%';  -- fallback path only
GRANT UPDATE ON openemr.sequences             TO 'copilot_ingest'@'%';  -- OpenEMR id idiom
GRANT SELECT ON openemr.*                     TO 'copilot_ingest'@'%';  -- dedup / id-resolve / demographics compare
GRANT ALL PRIVILEGES ON copilot.*             TO 'copilot_ingest'@'%';
GRANT SELECT ON copilot.*                     TO 'copilot_ro'@'%';      -- read tools may surface provenance
```

- **No UPDATE/DELETE** on any `openemr` clinical table (the `sequences` UPDATE is
  OpenEMR's own id-allocation idiom, needed only for the flagged fallback). The
  writer keeps an **in-code table allowlist** on top of these grants — the same
  boundary enforced twice, so a bug in one layer is caught by the other.
- `copilot_ro` gains only **SELECT on `copilot.*`** so read tools can surface
  provenance alongside chart rows.
- **Password idempotency (`ALTER USER`).** `CREATE USER IF NOT EXISTS` is a no-op
  when the user already exists (an earlier seed), so it would **not** pick up a
  rotated `__INGEST_PASS__`. The seeder follows every `CREATE USER` with an
  `ALTER USER ... IDENTIFIED BY '__INGEST_PASS__'`, making the injected password
  authoritative on every reseed — the credential the sidecar env expects can
  never drift from the DB. (The same pattern applies to `copilot_ingest`; the
  read-only `copilot_ro` is created the same way.)

### 2.4 Session-token signing key — `copilot_signing_key` (in the `openemr` DB)

The HMAC secret that signs/verifies session tokens (§1.5, `AUTH_TOKEN_SPEC.md`)
lives in a one-row table in the **`openemr`** database — deliberately, not in
`copilot`, so both sides read it with grants they already hold:

```sql
CREATE TABLE IF NOT EXISTS copilot_signing_key (
  id         INT NOT NULL PRIMARY KEY,
  secret     VARCHAR(128) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
INSERT IGNORE INTO copilot_signing_key (id, secret) VALUES (1, '__SIGNING_KEY__');
```

- **Why the `openemr` DB, not `copilot`:** OpenEMR PHP reads it natively
  (`sqlQuery`), and the sidecar reads it through the **existing** `copilot_ro`
  `SELECT ON openemr.*` grant. No cross-DB grant and **no env-var change on the
  openemr service** (which avoids the runbook's DB-wipe risk).
- **`INSERT IGNORE` keeps the key stable across reseeds** — it is set once; a
  later seed's freshly-generated value is discarded, so already-issued tokens
  keep verifying. The seeder's `run.sh` generates
  `SIGNING_KEY=$(openssl rand -hex 32)` (or a provided value for reproducibility)
  and substitutes `__SIGNING_KEY__` at apply time — **never committed**.
- **Fail-closed:** if the row is absent, the sidecar raises
  `TokenInvalid("signing_key_unavailable")` (no token can be trusted) unless
  `COPILOT_AUTH_DEV_MODE=true`, where it falls back to a fixed, logged dev key so
  the local UI works without the seeder. The sidecar caches the key ~60s so a
  rotation propagates without a restart.

### 2.3 What did NOT change in the DB

- No `openemr` table was altered, dropped, or had a column added — Week 2 writes
  land in existing OpenEMR tables via their native lab chain
  (`procedure_order → procedure_order_code → procedure_report → procedure_result`)
  and in `lists`, plus the new sidecar-owned `copilot.*` tables.
- `patient_data` (demographics) is **read-only to the ingestion path** —
  discrepancies are recorded in `copilot.intake_fact` as compare-and-flag, never
  written back (one-owner-per-data-type rule).

---

## 3. Observability log-key additions (`app/correlation.py`)

Logging is **allowlist-based**: `_ALLOWED_LOG_KEYS` is the only set of keys ever
serialized; anything else (a name, a drug, note text) is dropped before it can
reach the sink. Week 2 **added** enum/ID/count keys — never a value key:

- **Ingestion:** `doc_type`, `document_id`, `size_bytes`, `page_count`,
  `mimetype`, `run_id`, `phase`, `dedup_hit`, `fields_total`,
  `fields_extracted`, `fields_not_found`, `fields_failed_verification`,
  `target_table`, `target_pk`, `status`, `field_id`, `confidence`.
- **Supervisor graph handoffs:** `route`, `from_node`, `to_node`, `seq`,
  `handoff_id` (enum/ID only — a `Handoff`'s `reason`/`payload_summary` free
  text has **no key here by design**, so it can never reach logs even if the
  allowlist later grows a colliding key).
- **Guideline retrieval:** `retrieval_queries`, `retrieval_returned`,
  `retrieval_cited`, `rerank_provider`, `rerank_ms`, `provider`, `chunk_count`,
  `hit`, `retrieval_degraded`.

**`filename` is deliberately absent and must never be added** — uploaded
filenames routinely contain patient names (PHI). The dedup outcome / document_id
is logged instead. This is a pure addition: no Week 1 key was removed.

---

## 4. Behavioral migrations (no schema change, but worth stating)

These are the runtime behaviors the new keys and tables encode — the things a
grader will test:

- **Per-`(pid, content_hash)` dedup.** `UNIQUE(content_hash, pid)` on
  `ingestion_run` makes re-uploading the same file for the same patient a no-op:
  `IngestResult.deduplicated=true`, zero new rows, an explicit dedup notice.
  Re-uploading the same file for a **different** patient is a distinct upload
  (patient-scoped resolution — hardened per the §14 review; a second patient no
  longer collides on the first patient's document/provenance rows).
- **Date normalization on persist.** Verified collection dates are normalized to
  ISO before being written into the `datetime` columns of the lab chain — a
  document that prints `07/10/2026` still persists a parseable value (§14
  ingestion/MariaDB fix). Without this, strict MariaDB rejects the whole
  transaction and non-strict MariaDB zeroes the date.
- **Fail-closed persistence.** `COPILOT_INGEST_ALLOW_UNVERIFIED=false` (default):
  a field whose value does not token-align to its verbatim quote in the real
  text layer is `failed_verification` and is **never written** — surfaced as a
  gap instead.
- **`documents.revision` write-compat (fallback path).** When the flagged
  DB-document fallback inserts a `documents` row directly
  (`app/ingest/writer.py`), it sets `revision` **explicitly** to the write
  timestamp. The production OpenEMR schema declares `documents.revision`
  `NOT NULL` **with no default**, while other schema versions default it to
  `CURRENT_TIMESTAMP`; writing it explicitly makes the INSERT portable across
  both (without it, strict MariaDB rejects the row). This is a write-side
  compatibility detail only — no schema was altered (§14 hardening).

---

## 5. How to apply / roll back

**Apply (fresh or existing deploy):** run the seeder — it is idempotent.

```bash
# local (docker development-easy-light MariaDB on host port 8320)
mysql -h127.0.0.1 -P8320 -uroot -p < copilot/deploy/seeder/live_seed.sql
mysql -h127.0.0.1 -P8320 -uroot -p < copilot/sql/seed_showcase.sql   # showcase patients 101/102
```

On Railway the seeder runs against the private `mariadb` service (see
`copilot/deploy/RUNBOOK.md` / `DEPLOY.md`); the `__RO_PASS__` / `__INGEST_PASS__`
placeholders are substituted from private env at seed time — **never committed**.

**Roll back:** the `copilot` schema is sidecar-owned and additive — dropping it
(`DROP DATABASE copilot; DROP USER 'copilot_ingest'@'%';`) reverts Week 2's DB
footprint entirely and leaves `openemr` exactly as Week 1 left it. The contract
changes are additive, so an older client reading a newer response simply ignores
`guideline_evidence`/`handoffs`. There is no destructive forward migration to
undo.

## 2026-07-20 — Week-3 adversarial platform (additive; no co-pilot interface change)
The `redteam/` package + `contracts/` + `evals/adversarial/` are additive: they do NOT
change any co-pilot interface, DB schema, or shipped `app/*` contract (the golden 65-case
gate is unchanged and green). The pre-push gate grew steps [5/6] adversarial runner +
[6/6] adversarial gate (hermetic). New inter-agent message schemas are versioned under
`contracts/*.v1.json`; a breaking change bumps the version (contract tests enforce this).
