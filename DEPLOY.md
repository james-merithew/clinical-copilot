# Railway Deployment (OpenEMR fork)

Backs **Task 2** of the MVP plan in [CLAUDE.md](CLAUDE.md): the OpenEMR fork
running on a live public URL, matching the CLAUDE.md topology (only the web
service is public; the database is private).

## Live instance

- **Public URL:** https://openemr-production-31ad.up.railway.app
- **Login:** `admin` / _(strong generated password — stored in Railway env, shared out-of-band, never in this repo)_
- **Railway project:** `openemr-copilot` (workspace: personal), environment `production`

## Topology

| Service | Source | Exposure | Persistence |
| --- | --- | --- | --- |
| `openemr` | **fork image** (`copilot/deploy/openemr-image/`: stock 8.2.0 + Co-Pilot module + baked `sqlconf.php`) | **public** (Railway domain → port 80, TLS at edge) | config baked in image; data in MariaDB |
| `mariadb` | `mariadb:11.8.8` | **private** (`mariadb.railway.internal`) | volume at `/var/lib/mysql` |
| `copilot` | FastAPI sidecar (`copilot/`) | public demo domain (patient endpoints require an OpenEMR-minted session token; `/health` + `/ready` open — see `copilot/AUTH_TOKEN_SPEC.md`) | stateless |
| `copilot-seeder` | idempotent demo-data seeder (`copilot/deploy/seeder/`) | private, run-on-demand | n/a |

**Restart-hardening (fixed a real incident):** the stock image's entrypoint re-ran the auto-installer
on every restart (ephemeral `sites/`, `$config = 0`) and **wiped the DB**. The fork image bakes
`sites/default/sqlconf.php` with `$config = 1` (creds via `getenv()` from Railway vars) +
`MANUAL_SETUP=yes`, so the installer path is unreachable; boots go straight to Apache against the
persistent MariaDB. Verified restart-proof (2× forced redeploys, zero data loss). Ops details:
`copilot/deploy/RUNBOOK.md`.

Secrets (DB root/app passwords, admin password, API keys) live **only in Railway
environment variables**, never in the repo. `MYSQL_*` on the `openemr` service
point at the private MariaDB.

## Railway-specific gotchas (why the config looks the way it does)

1. **Private networking is IPv6-only.** MariaDB must bind IPv6 or the web
   service can't reach it. The `mariadb` service start command is:
   ```
   docker-entrypoint.sh mariadbd --bind-address=* --character-set-server=utf8mb4
   ```
2. **Railway bypasses the image `ENTRYPOINT`** when a custom start command is
   set — so the MariaDB start command must call `docker-entrypoint.sh` itself,
   or the DB is never initialized and `mariadbd` aborts trying to run as root.
   (Same pattern Railway documents for the Mongo image.)
3. **No `/sites` volume.** Railway volumes are bind-mounts and do **not** copy
   the image's baked-in `sites/default` skeleton into an empty volume the way
   Docker named volumes do. Mounting an empty volume over
   `/var/www/localhost/htdocs/openemr/sites` hides the skeleton and sends the
   image into an `Upgrade detected: 0 -> 12` crash loop. We currently run with
   **ephemeral `/sites`** — see gaps below.

## Redeploy

Image-based services redeploy when a variable changes. Either set/patch a
variable (Railway MCP `set_variables`, or the dashboard), or use the CLI:
```
railway service redeploy -s openemr -e production -p <project-id>
```

## Known gaps / tracked follow-ups

- **Live clinic is empty (no demo patients).** `DEMO_MODE=standard` is honored
  by the OpenEMR **flex** dev image (used locally — see [RUN_LOCAL.md](RUN_LOCAL.md),
  which loads the 3 demo patients) but **not** by the production
  `openemr/openemr:8.2.0` image, so the Railway install came up empty. Loading
  demo/synthetic patients into the live instance (via DB import or Synthea) is a
  tracked follow-up.
- **`/sites` is ephemeral.** OpenEMR config + generated encryption keys live in
  the container layer and reset on redeploy. Proper fix: a start command that
  seeds an attached volume from the image's `sites` skeleton on first boot.
- **OAuth signing keys not generated.** `GET /meta/health/readyz` reports
  `oauth_keys: false`; needed for the FHIR/OAuth2 path the sidecar (Task 4) uses.

## Deployment mechanics

Driven through the **Railway MCP server** (see [CLAUDE.md](CLAUDE.md) setup note)
and the Railway CLI. The project was built service-by-service (create project →
`mariadb` service + start command + root password + volume → `openemr` service +
`MYSQL_*`/`OE_*` vars + generated domain), not via docker-compose — Railway
deploys services individually over its private network.
