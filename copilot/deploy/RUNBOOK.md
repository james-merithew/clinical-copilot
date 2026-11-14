# Co-Pilot Ops Runbook (demo / presentation)

## Topology (Railway project `openemr-copilot`, env `production`)
| Service | What | Source |
|---|---|---|
| `openemr` | OpenEMR + baked Co-Pilot module + **baked sqlconf.php** | `copilot/deploy/openemr-image/` |
| `mariadb` | System of record (persistent volume at `/var/lib/mysql`) | image `mariadb:11.8.8` |
| `copilot` | FastAPI sidecar (agent + panel), public demo domain | `copilot/` |
| `copilot-seeder` | One-off idempotent demo-data seeder (private) | `copilot/deploy/seeder/` |

## Why restarts are safe now
The OpenEMR image bakes `sites/default/sqlconf.php` with `$config = 1` (creds read from env at include
time) + `MANUAL_SETUP=yes`. The entrypoint's auto-installer only runs when `$config == 0`, so the
re-install-and-wipe path can never fire. Verified: two forced `openemr` restarts + one `copilot`
restart with zero data loss.

## ⚠️ Do NOT change env vars on the `openemr` service
Setting/changing a variable on `openemr` makes Railway redeploy from the service's image source,
which can revert the baked `sqlconf.php` and **re-run the installer (wipes the DB)**. A plain
`railway redeploy` (or a platform restart) is safe — it re-runs the current baked image. Rule of thumb:
`railway up -c` and `railway redeploy` on openemr = safe; `railway variables --set` on openemr = danger.
The clinic timezone is set in the **database** (`globals.gbl_time_zone`, seeded), NOT via a `TZ` env var,
precisely so we never have to touch openemr's variables.

## One-command recovery (if demo data ever looks wrong OR after an accidental wipe)
If openemr ever reinstalls, run BOTH: re-establish the baked image, then re-seed.
```powershell
# 1) make the baked ($config=1) image the current deploy again
cd copilot\deploy\openemr-image
railway link -p 99ee8234-bb88-42e2-85b1-c337a45ac33d -e production -s openemr
railway up -c
# 2) restore demo data (idempotent)
```
Then the standard re-seed:
```powershell
cd copilot\deploy\seeder
railway link -p 99ee8234-bb88-42e2-85b1-c337a45ac33d -e production -s copilot-seeder
railway up -c     # idempotent: RO user, patients 101-110 + 120, vitals/labs, 14-day schedule, module row
```
(Use PowerShell — the git-bash CLI has a subdirectory bug. Look for `SEED_OK` in the service logs.)

## Pre-presentation checklist (5 minutes)
1. `GET https://copilot-production-5e86.up.railway.app/health` → `alive`; `/ready` → `status: "ready"` with every check true (core `database`/`llm`/`observability` plus `vector_index`, `reranker`, and the ingest trio when enabled); `degraded` is survivable but investigate the false check.
2. Open `https://copilot-production-5e86.up.railway.app/` → loads **Marcus Delgado** by default; alerts +
   metric cards render; **My Day** lists 10 patients for *your local* date.
3. Log into live OpenEMR (`admin` + the `OE_PASS` Railway var) → open Marcus Delgado → the **Clinical
   Co-Pilot card renders in the chart**, scoped to him.
4. Send one chat turn ("Brief me on this patient") → verified, cited briefing returns.
5. If anything is off: run the recovery command above; if OpenEMR itself is down, `railway redeploy -s
   openemr -y` (safe — restart-proof) and re-check in ~90s.

## Cohesion (OpenEMR UI shows what the Co-Pilot shows)
The seed writes the `forms` wrappers (`formdir='newpatient'`/`'vitals'`) and `procedure_order_code`
rows that OpenEMR's own screens require — so encounters, vitals, and labs are visible **in the chart**,
not just to the sidecar. It also makes the login user **Dr. Sarah Chen, MD** (a real calendar provider)
and sets the day's schedule. The seeder self-verifies at the end; a healthy run prints:
`doctor=Sarah Chen | enc_wrappers=5 | vit_wrappers=5 | lab_codes=3 | appts_today=10 | p120_provider≠1`.

In-EMR entry points: **Co-Pilot** main-menu item → My Day; open any patient → the **Clinical Co-Pilot**
card at the top of the chart (collapsible, auto-scoped, fresh per patient — no cross-patient state).

## Notes
- The seeder schedule is a rolling **±14-day window from the seed run** — re-run the seeder if the demo is
  more than ~13 days after the last seed.
- Demo patients: 101 Marcus Delgado (briefing/trends/divergence), 102 Eleanor Whitfield (critical
  allergy + DDI), 103–110 (My Day panel), 120 Rachel Osei (authz-denied fixture, other provider).
- Login user = Dr. Sarah Chen (OpenEMR `admin`). All data is synthetic. Keys/creds in Railway vars only.
