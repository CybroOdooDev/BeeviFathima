# BioBridge on Docker

One image (`deploy/Dockerfile`), one compose file, one production override.
Everything below runs from the project root. Needs Docker with Compose v2.24
or newer (`docker compose version`).

| File | What it is |
|---|---|
| `deploy/docker-compose.yml` | Postgres + the API (which also runs the sync schedule). Local use: dashboard on `http://localhost:8000`. |
| `deploy/docker-compose.prod.yml` | Adds Caddy (automatic HTTPS on your domain, a plain-HTTP port for ZKTeco push terminals) and a nightly database backup; the API is no longer published directly. |
| `deploy/.env.example` | Values compose itself needs (`POSTGRES_PASSWORD`, domain…). Copy to `deploy/.env`. |
| `deploy/.env.production.example` | The app's settings for a server. |
| `tools/sqlite_to_postgres.py` | One-off copy of an existing `biobridge.db` into the stack's Postgres. |

The app reads its settings from an env file — the project's `.env` by default,
or whatever `BIOBRIDGE_ENV_FILE` (in `deploy/.env`) names. `DATABASE_URL` in it
is ignored: the stack always uses its own Postgres.

## 1. On your PC

```bash
cp deploy/.env.example deploy/.env          # set POSTGRES_PASSWORD
docker compose -f deploy/docker-compose.yml up -d --build postgres
```

Bring your current data across (accounts, connections, punches, history).
Stop the old `uvicorn` first so nothing writes to the file during the copy:

```bash
docker compose -f deploy/docker-compose.yml --profile import run --rm import-sqlite
```

It brings a copy of `biobridge.db` to the latest schema, copies every table in
one transaction, and checks that the stack's `MASTER_ENCRYPTION_KEY` can read
the stored Odoo / device passwords. Your `.env` is reused, so the key is the
same and they are. It also points connections at `127.0.0.1` / `localhost`
to `host.docker.internal`, because inside a container `127.0.0.1` is the
container itself. The original file is never changed.

Then start the app:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f api
```

Open http://localhost:8000 and sign in as before.

**The mock BioTime** runs in the stack too, so it reads the tenant's roster
from the same Postgres (one left running on the host would keep reading the
old `biobridge.db`). Stop the host one, then:

```bash
# deploy/.env: MOCK_TENANT=cybrotech  MOCK_PORT=8100  MOCK_TZ=Asia/Kolkata
docker compose -f deploy/docker-compose.yml --profile mock up -d mock-biotime
```

Check what it started with — the first lines say where its people came from and
how often it punches:

```bash
docker compose -f deploy/docker-compose.yml logs mock-biotime | head -20
# "read from Odoo" = your employees.  "built-in fixture" / "BUILT-IN SAMPLE PEOPLE" =
# the mock's own sample badges (1001, 2001, 5, 6001 …, terminals GATE-01..06): wrong.
# "SIMULATING: one punch every 20s" is the default; add --interval to change it.
```

If the mock cannot read this account's saved Odoo connection it now stops with the
reason instead of falling back to those samples. It is published on port 8100 of your PC, so the imported connection
(`http://host.docker.internal:8100`) reaches it unchanged; its data stays in
`mock_data/`. A second tenant on another port:

```bash
docker compose -f deploy/docker-compose.yml --profile mock run -d -p 8099:8099 \
    mock-biotime --tenant other-slug --port 8099 --tz Asia/Dubai
```

**Anything else on your PC the containers must reach** (a local Odoo, say)
has to listen on all addresses, not only `127.0.0.1`, and is addressed as
`http://host.docker.internal:<port>`. If a host firewall (ufw) is on, allow
the Docker bridge to reach those ports.

**ZKTeco push terminals on your LAN** keep pointing at this PC's IP and port
8000, as now (`ADMS_SERVER_HOST` / `ADMS_SERVER_PORT` in `.env`).

## 2. On a server

1. A Linux server with Docker, a DNS A record (e.g. `app.biobridge.example`)
   pointing at it, ports 80, 443 and the ADMS port (8081) open.
2. Copy the project to `/opt/biobridge`.
3. App settings: `cp deploy/.env.production.example .env` and fill it in. Generate
   `JWT_SECRET` and `MASTER_ENCRYPTION_KEY` with
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Production
   refuses to start with the development keys.
4. Compose settings: `cp deploy/.env.example deploy/.env` and set
   `POSTGRES_PASSWORD`, `APP_DOMAIN`, `ACME_EMAIL`, `ADMS_PORT`.
5. Start:

   ```bash
   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
   ```

Caddy gets the certificate on first start. `https://APP_DOMAIN` serves the
dashboard, API and staff console; `http://APP_DOMAIN:8081/iclock/…` serves
only the ZKTeco push endpoint (terminals can't do HTTPS), nothing else.

**Moving your PC's data to the server** — the server has a new
`MASTER_ENCRYPTION_KEY`, so pass the old one and every stored secret is
re-encrypted on the way in:

```bash
# deploy/.env on the server:
SQLITE_FILE=/opt/biobridge/biobridge.db
OLD_MASTER_ENCRYPTION_KEY=<the key from your PC's .env>

docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.prod.yml \
    --profile import run --rm import-sqlite
```

Remove `OLD_MASTER_ENCRYPTION_KEY` from `deploy/.env` afterwards. Connections
that pointed at your PC (`host.docker.internal`, mock servers) won't work from
the server — edit or remove them.

**Backups**: a dump every 24 hours in `deploy/backups/`, 14 days kept. Restore:

```bash
gunzip -c deploy/backups/biobridge-YYYYMMDD-HHMM.sql.gz | \
  docker compose -f deploy/docker-compose.yml exec -T postgres psql -U biobridge biobridge
```

## Everyday

```bash
C="docker compose -f deploy/docker-compose.yml"          # add -f deploy/docker-compose.prod.yml on the server
$C ps                         # what is running, and healthy
$C logs -f api                # the app and its scheduler
$C up -d --build              # deploy new code: migrations run before the API starts
$C exec api python tools/grant_admin.py --email you@example.com   # staff access
$C exec postgres psql -U biobridge biobridge             # a SQL prompt
```

`/health` is liveness (the container healthcheck); `/health/scheduler` says
whether syncs are being dispatched — watch that one from monitoring.
