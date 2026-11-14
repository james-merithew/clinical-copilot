# Clinical Co-Pilot — API Collection

A runnable **Bruno** collection that exercises every endpoint of the Co-Pilot
sidecar. A grader can drive the full system — health, readiness, both `/chat`
paths, the authorization denial, and the **Week 2 multimodal flow** (document
upload → viewer → "what changed?") — **without reading any source code**.

> Primary: **Bruno** (the `.bru` files + `bruno.json` in this folder).
> Also included: an equivalent **Postman v2.1** export
> (`clinical-copilot.postman_collection.json`) if you prefer Postman.

## What's in here

| # | Request | Method | What it proves |
|---|---|---|---|
| 01 | Health (liveness) | `GET /health` | Process is up. Always 200, no auth, no dep checks. |
| 02 | Ready (dependency check) | `GET /ready` | Pings **database + llm + observability**; 200 only when all healthy, else 503 with a per-check breakdown. |
| 03 | Chat — free text (pid 101) | `POST /chat` | Source-attributed briefing for Showcase A; Metformin `prescriptions`↔`lists` divergence + A1c trend. |
| 04 | Chat — chip `care_gaps` (pid 102) | `POST /chat` | Chip tap = first-class turn; constraint flags (penicillin↔amoxicillin allergy, warfarin+ibuprofen). |
| 05 | Chat — authz denial (pid 23) | `POST /chat` | Authorization Gateway returns **403** before any read (fail-closed). |
| 06 | Ingest — lab PDF (pid 101) | `POST /ingest` | Week 2 document ingestion (multipart); IngestResult with citations, gaps (fail-closed), dedup. Sets `documentId`. |
| 07 | Document meta (pid 101) | `GET /documents/{id}/meta` | Page dims for the overlay; per-request document↔patient ownership check. |
| 08 | Document page PNG (pid 101) | `GET /documents/{id}/page/{n}.png` | Server-rendered page image (ownership checked before the cache). |
| 09 | Document citations (pid 101) | `GET /documents/{id}/citations` | Per-field DocumentRef provenance (bbox + verbatim quote) — the click-to-source targets. |
| 10 | Metrics summary | `GET /metrics/summary` | PHI-free dashboard counters: per-endpoint/per-node p50/p95, ingestion pass rate, retrieval, evals. |
| 11 | Chat — what changed (pid 101) | `POST /chat` | Full Week 2 flow (run 06 first): document facts in the briefing, `handoffs[]`, separate `guideline_evidence[]`. |
| 12 | Ingest — medication list PDF (pid 101) | `POST /ingest` | Third doc type: verified med rows land in `lists` (`type='medication'`), **never** `prescriptions`; Discontinued row → `activity=0`; missing frequency stays a stated gap. Sets `documentId`. |
| 13 | Labs trend (pid 101) | `GET /labs/trend` | Deterministic per-test series for the chart widget: chronological ASC, every point cites its `procedure_result` PK; optional `test` filter; degrades to empty series + notes (never 500). |

**Week 2 requests (06–12)** need the sidecar running with
`COPILOT_INGEST_ENABLED=true` and the `copilot` schema seeded
(`deploy/seeder/live_seed.sql`). Run **06 first** — it uploads
`../tests/ingest/fixtures/demo_lab_delgado.pdf` for pid 101 and stores the new
`document_id` into the `documentId` variable, which requests 07–09 consume.

## Collection variables

Defined at the collection level (in `collection.bru`) **and** in the `Local`
environment, so requests run out of the box:

| Variable | Default | Used for |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:8808` | Every request URL. Point at the live Railway URL to test the deployed sidecar. |
| `token` | *(empty)* | `Authorization: Bearer {{token}}` on the `/chat`, `/ingest`, viewer, and `/metrics/summary` requests. Populated by request **00 Mint dev token**. |
| `documentId` | `1` | Path param for the document viewer (07–09). Request 06 overwrites it with the real id from the ingest response. |

**Auth (Week 2):** the reusable bearer is **revoked** — every authenticated
request now sends a short-lived, OpenEMR-minted **session token**. Run **`00 Mint
dev token`** first: it hits `GET /dev/token` and stores the result in `{{token}}`.
That endpoint exists only when the sidecar runs with `COPILOT_AUTH_DEV_MODE=true`
(dev only — it is **404 in production**). Against a real deployment, obtain a
token from the OpenEMR Co-Pilot panel (minted after a real
`aclCheckCore('patients','demo')`) and paste it into `{{token}}`. Note the token
is **patient-bound**: the pid-101 token from request 00 is correctly **refused
(403)** by the pid-102 (04) and pid-23 (05) requests — that is the token↔patient
binding, not a failure. `/health` and `/ready` are unauthenticated by design.

## Run it in Bruno

1. Install Bruno (desktop app: <https://www.usebruno.com>, or the `bru` CLI:
   `npm install -g @usebruno/cli`).
2. **Open Collection** → select this `api-collection/` folder. The requests
   appear in sequence order (01–05 Week 1, 06–12 Week 2).
3. (Optional) Top-right environment selector → choose **Local**. Not required —
   the same `baseUrl` default is set at the collection level too. To hit the
   deployed sidecar, edit `baseUrl` to the public Railway URL (and paste a real
   OpenEMR-minted token into `{{token}}`).
4. Make sure the sidecar is running and reachable at `baseUrl` (locally:
   `uvicorn app.main:app --port 8808` from `copilot/`, with the demo data seeded).
5. Click a request → **Send**. Each request has inline `docs` explaining what to
   look for, and lightweight `assert`s (status code + key fields) so a run is
   self-checking.

### Run the whole collection from the CLI

```bash
# from copilot/api-collection/
bru run --env Local
```

This executes the requests in order and reports the assertions. Requests 01–04
should pass; requests **04 and 05 assert 403** — 04 the token↔patient binding
(pid-101 token on pid 102), 05 the provider-scope denial (a passing assert means
access control fired correctly). Week 2 requests 06–12 need
`COPILOT_INGEST_ENABLED=true` + the seeded `copilot` schema, and 07–09 depend on
06 having run first (it sets `documentId`).

## Run it in Postman

**Import** → drop in `clinical-copilot.postman_collection.json`. The `baseUrl` and
`token` collection variables are set at the collection level; edit them under the
collection's **Variables** tab to target the deployed instance.

## Notes for the grader

- The requesting **identity is pinned server-side** — there is deliberately no
  `user_id` in any request body. The client may only choose `pid`. That is why the
  denial in request 05 is enforced by the server, not by anything the caller sends.
- `pid 101` / `pid 102` are the two curated showcase patients; `pid 23` is the
  denial fixture (owned by a different provider). All data is synthetic.
- If `/ready` returns 503, check its per-check breakdown — a missing
  `ANTHROPIC_API_KEY` or `LANGSMITH_API_KEY` will show exactly which dependency is
  unresolved.
