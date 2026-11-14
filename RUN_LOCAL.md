# Running OpenEMR Locally (Clinical Co-Pilot dev)

This is the canonical way to bring up the OpenEMR fork on a dev machine with
**demo data** loaded. It backs **Task 1** of the MVP plan in
[CLAUDE.md](CLAUDE.md). OpenEMR remains the system of record; the Co-Pilot
sidecar is added later (Task 4) into this same topology.

## What we run

We use the repo's built-in **`docker/development-easy-light`** Compose stack.
It is the lightest official OpenEMR dev environment that still auto-installs
with demo data, and it pre-enables the REST + FHIR + OAuth2 settings the
sidecar will need later.

Three services:

| Service      | Image                 | Purpose                                  |
| ------------ | --------------------- | ---------------------------------------- |
| `openemr`    | `openemr/openemr:flex`| The OpenEMR web app (Apache/PHP)         |
| `mysql`      | `mariadb:11.8`        | System-of-record database (demo data)    |
| `phpmyadmin` | `phpmyadmin:5.2`      | DB inspection (used for the AUDIT checks) |

`EASY_DEV_MODE=yes` makes the OpenEMR container **auto-install** on first boot
(no manual setup wizard). `DEMO_MODE=standard` (added to the `openemr` service)
seeds OpenEMR's **standard demo dataset** — sample patients, encounters,
medications, problems, and pre-configured ACL users. Without `DEMO_MODE` you get
a valid but **empty** clinic (admin login only, zero patients).

> Demo data is loaded **only on a fresh install** (empty `databasevolume`). If
> you brought the stack up before adding `DEMO_MODE`, re-seed with
> `docker compose down -v` then `docker compose up -d`.

> The heavier `docker/development-easy` stack additionally bundles Selenium,
> CouchDB, OpenLDAP, and Mailpit. We don't need those for the MVP; use the
> `-light` stack unless a feature specifically requires them.

## Prerequisites

- **Docker Desktop** (includes Docker Engine + Compose v2).
  - Windows: requires WSL 2. Docker Desktop installs/enables it.
  - Verify: `docker --version` and `docker compose version` both succeed,
    and `docker info` shows a running server.

## Start

From the repo root:

```bash
cd docker/development-easy-light
docker compose up -d
```

First boot pulls images and runs the auto-install — **allow ~5–10 minutes**.
Watch progress until it reports ready:

```bash
docker compose logs -f openemr
# Wait for the install to finish; the container healthcheck hits
# https://localhost/meta/health/readyz internally.
docker compose ps        # openemr should show "healthy"
```

## Access

| URL                     | What                       | Credentials       |
| ----------------------- | -------------------------- | ----------------- |
| http://localhost:8300   | OpenEMR (HTTP)             | `admin` / `pass`  |
| https://localhost:9300  | OpenEMR (HTTPS, self-signed)| `admin` / `pass`  |
| http://localhost:8310   | phpMyAdmin                 | see DB creds below |

**Database (from the host, port 8320):**

| Field    | Value      |
| -------- | ---------- |
| Host     | `127.0.0.1` port `8320` |
| Root     | `root` / `root` |
| App user | `openemr` / `openemr` |
| Schema   | `openemr`  |

> Ports are overridable via `WT_HTTP_PORT`, `WT_HTTPS_PORT`, `WT_PMA_PORT`,
> `WT_MYSQL_PORT` env vars if any conflict on your machine.

Additional demo **ACL users** (for testing OpenEMR's gacl authorization — the
`admin` password is `pass`; the other demo users' passwords are listed on the
[OpenEMR Development Demo wiki](https://www.open-emr.org/wiki/index.php/Development_Demo#Demo_Credentials)):
`physician` (Donna Lee), `clinician` (Fred Stone), `receptionist`
(Barbara Wallace), `accountant` (Ernie Stent).

## Verifying the install

The install is confirmed when the `openemr` container reports `healthy`
(`docker compose ps`) and the demo data is present:

```bash
docker exec development-easy-light-mysql-1 \
  mariadb -uroot -proot openemr -N -e \
  "SELECT 'patients', COUNT(*) FROM patient_data
   UNION ALL SELECT 'encounters', COUNT(*) FROM form_encounter
   UNION ALL SELECT 'prescriptions', COUNT(*) FROM prescriptions
   UNION ALL SELECT 'lists_meds', COUNT(*) FROM lists WHERE type='medication';"
```

A freshly seeded standard demo set yields **3 patients** (Phil Belford,
Susan Underwood, Wanda Moore), 3 encounters, and — usefully for the AUDIT
work — medications split across two tables (`prescriptions` vs
`lists type='medication'`), the exact divergence `AUDIT.md` calls out.

> **Known gap (matters for the sidecar, Task 4, not for running locally):**
> `GET /meta/health/readyz` reports `oauth_keys: false` on a fresh install —
> the OAuth2 signing keys aren't generated until first needed. The FHIR/OAuth2
> path the Co-Pilot uses will require generating these; track it there.

## Stop / reset

```bash
# Stop (keeps the database volume — demo data persists)
docker compose down

# Full reset (WIPES the database and all volumes — fresh demo install next up)
docker compose down -v
```

## Troubleshooting

- **Port already in use:** set the `WT_*_PORT` override for the conflicting
  service and `docker compose up -d` again.
- **`openemr` never turns healthy:** check `docker compose logs openemr` — the
  first-boot install can take several minutes; the healthcheck `start_period`
  is 3 minutes before it begins reporting.
- **Docker daemon not running:** start Docker Desktop and wait for the whale
  icon to settle before retrying.
