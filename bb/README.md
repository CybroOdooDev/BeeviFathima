# BioBridge

Multi-tenant middleware that pulls biometric punches from a customer's ZKTeco
BioTime server and writes clean `hr.attendance` records into their Odoo.

Traffic runs outward in both directions — BioBridge *polls* BioTime and *writes*
to Odoo — so the customer installs no agent and no Odoo module. They expose a
BioTime port and issue an Odoo API key.

```
ZKTeco devices → BioTime (customer LAN) ──REST──▶ BioBridge ──XML-RPC──▶ Odoo
```

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate          # do this first — see the note below
pip install -r requirements-dev.txt

cp .env.example .env               # defaults are fine for local work
uvicorn app.main:app --reload      # dashboard on http://localhost:8000/
```

Open `http://localhost:8000/` and create an account — that is the dashboard.
`/docs` is the same API as OpenAPI, for anyone integrating.

Outside production the app creates its tables on startup, so a fresh checkout
boots into a working system. Production does not: point `DATABASE_URL` at
Postgres and run the schema step yourself, once.

```bash
python3 tools/init_db.py           # create the tables (--drop wipes first)
```

`create_all` only adds missing tables. It never alters or drops one, so it
cannot migrate a schema that has already changed under live data — that is what
Alembic is for, and why production does not run this implicitly.

Activate the virtualenv before anything else. Ubuntu 24.04 and most current
distributions ship `python3` with no `python` alias, and a system `pip install`
is blocked by PEP 668; the venv gives you both a working `python` and somewhere
to install. Without it you get `No module named pytest` while `uvicorn` appears
to work, because it resolved from some other project on the system.

Install `requirements-dev.txt`, not `requirements.txt` — the dev file pulls in
the runtime dependencies *and* pytest. `requirements.txt` alone is the
production set, with no test tooling, which is the point of the split.

If port 8000 is taken (`[Errno 98] Address already in use`), find the holder
with `ss -ltnp | grep :8000` and either stop it or run on another port:

```bash
uvicorn app.main:app --reload --port 8001
```

SQLite and no broker out of the box, so there is no database or queue to set up.
With no Celery worker running, the sync endpoints run the cycle **inline** rather
than queueing into a void — a deployment without a worker still works, it just
does the sync inside the request.

Try the whole loop with no hardware:

```bash
python3 tools/mock_biotime.py --punches punches.json   # fake BioTime on :8099
python3 tools/e2e_proof.py --odoo-url https://your.odoo.com \
    --odoo-db yourdb --odoo-user you@example.com --odoo-key <api-key>
```

## Tests

```bash
source .venv/bin/activate     # or: .venv/bin/python -m pytest
python -m pytest              # 56 tests
```

Coverage is aimed at what actually breaks in the field: pairing across sync
cycles, timezone conversion in four zones, ledger idempotency, cursor behaviour
when a provider fails mid-fetch, tenant isolation through the real HTTP stack,
and that a tenant's derived key cannot decrypt another tenant's secrets.

`tools/e2e_proof.py` goes further and drives the engine against a **live Odoo**
with no stubs on either side.

## How a sync run works

```
fetch → ingest → normalise → register → map → pair → push → record
```

Two invariants make a run safe to interrupt:

- The ledger is keyed on `(tenant_id, source_id, external_id)` — the vendor's own
  id, scoped to its source. Ingesting the same window twice is a no-op, so the
  re-read window costs nothing and a full re-pull is safe.
- The cursor advances only **after** punches are stored, and only to the newest
  punch actually seen. A crash costs a retry, never a gap.

### The one thing this gets right that a naive port does not

A check-in arrives in one cycle and the check-out in the next — that is the
normal case, because the whole point of polling every few minutes is that the
check-out has not happened yet.

Pair only the punches that are still pending and the second cycle sees a lone
check-out, pairs it as a *new open interval starting at the check-out time*, and
writes a phantom record. In `alternating` mode that phantom is left open, and the
hourly maintenance task then closes it at +16 h — a spurious 16-hour attendance,
every day, on every employee.

So `pair_punches` takes the employee's **currently open shift** as an input, read
from Odoo and reconciled with what we stored on the mapping row. A lone check-out
then does the only sensible thing: it closes the shift it belongs to.

```python
result = pair_punches(punches, config, open_shift=OpenShift(id, check_in))
# -> Interval(check_in=<recorded>, check_out=<this punch>, closes_attendance_id=id)
```

`tests/test_pairing.py::test_lone_checkout_closes_the_open_shift_instead_of_opening_one`
is the test that pins it.

## Layout

```
app/
  core/          config · Fernet envelope encryption · JWT and password hashing
  db/            declarative base, UUID and timestamp mixins, session factory
  models/        tenant · users · sessions · connections · devices · ledger
  integrations/
    base.py      the seam: AttendanceProvider, PunchEvent, Capability, registry
    providers/   biotime — add a vendor here, nothing above changes
    odoo.py      XML-RPC client with timeouts and actionable transport errors
  services/
    timeutils.py every timezone conversion, and nowhere else
    pairing.py   punch stream → intervals. Pure, no DB, no network
    sync_engine.py  the pipeline
    connections.py  the only module that decrypts credentials
  api/           deps (tenant isolation) · auth · connections · sync
  workers/       Celery app, beat schedule, per-tenant Redis lock
  static/        the dashboard — plain ES modules, no build step
tools/           mock BioTime server · schema init · end-to-end proof · UI smoke
```

## The dashboard

Served from `/app/`, with `/` redirecting there. Six screens:

| Screen | What it answers |
| --- | --- |
| Overview | Is it running, did the last sync work, what needs me |
| Attendance | What did BioBridge write, and did it land in Odoo |
| Activity | Every punch ever pulled, with its state and why |
| Employees | Which badges are stuck unmatched, and fix them here |
| Connections | Both credentials, a test button, the terminal list |
| Settings | Pairing mode, dedupe window, shift caps, working hours |

It is hand-written ES modules with no bundler, no framework and no `npm install`:
the files you edit are the files the browser runs. That is a deliberate trade —
this ships into customer infrastructure, and a build toolchain is one more thing
to keep alive for a six-screen admin UI. Routing is hash-based for the same
reason: it needs no rewrite rules from whatever proxy sits in front.

The access token is held in memory only; the refresh token sits in
`sessionStorage`, so a reload keeps you signed in and closing the tab does not.

Every screen states what it is about to do before it does it — the Odoo URL
field warns about the `/odoo` suffix that causes most failed connections, and the
server-timezone field warns that a wrong value shifts every attendance by hours
with no error anywhere.

`tools/ui_smoke.py` drives it in a real browser: sign up, connect both sides,
import terminals, run a sync, read every screen, and fail on any console error.

```bash
python3 tools/ui_smoke.py --base http://127.0.0.1:8000 \
    --odoo-url https://your.odoo.com --odoo-db yourdb \
    --odoo-user you@example.com --odoo-key <api-key> \
    --shot-dir /tmp/bbshots        # a screenshot per screen
```

**Elapsed is not Odoo's Worked Hours.** The attendance table shows punch-to-punch
time. From Odoo 17 onward `hr.attendance.worked_hours` subtracts the break in the
employee's working schedule, so a 9-hour span can read 8.0 there. Both numbers
are right; they measure different things.

## Design notes worth knowing

**Tenant isolation is one dependency.** `get_principal` derives the tenant from
the JWT and asserts the token's `tid` matches the user row. There is no row-level
security in the database, so every query filters on `principal.tenant.id` — an
unscoped query is a cross-tenant leak. Routes return **404**, not 403, for an
object belonging to another tenant, so an id's existence is never confirmed.

**Credentials are encrypted per tenant.** Fernet with a key derived via HKDF from
the master key, salted with the tenant id, version-tagged `v1:` for rotation. A
database dump decrypts to nothing, and one tenant's key cannot read another's
rows. `services/connections.py` is the only module that calls `decrypt`.

**Secrets are write-only by absence.** An API key appears on the input schema and
has no field on the output schema. There is no masking helper to forget to call.

**Timezones live in one module.** BioTime reports naive local wall-clock; Odoo
stores naive UTC. Getting it wrong does not raise — it produces plausible
attendance that is hours out, which is why `timeutils.py` has its own test suite.

**Failures are graded.** A configuration problem (`SyncAborted`) does not count
towards the failure streak, so a tenant mid-onboarding is never slow-laned. A
genuine outage does, and at five consecutive failures connections are marked
degraded and polling drops to a quarter rate.

**Per-source isolation.** Each device source is fetched in its own try/except. One
unreachable site does not skip every other site's punches — with several sources,
letting a failure propagate is data loss disguised as a failed run.

**Odoo's constraints are respected by construction.** At most one open attendance
per employee, no overlapping intervals, `check_in`/`check_out` naive UTC, and
`worked_hours` computed by Odoo — never written.

## Configuration

Everything is environment driven; see `.env.example`. The values that matter
most:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | SQLite file | Postgres in production |
| `REDIS_URL` | *(empty)* | Empty means no broker: syncs run inline |
| `MASTER_ENCRYPTION_KEY` | dev value | **Back this up.** Rotating it invalidates every stored credential |
| `JWT_SECRET` | dev value | |
| `FETCH_OVERLAP_MINUTES` | 15 | How far before the cursor to re-read |
| `ALLOW_PRIVATE_NETWORK_TARGETS` | true | Set false in a public deployment |
| `CORS_ORIGINS` | *(empty)* | Empty = same-origin only |

The app refuses to boot when `ENVIRONMENT=production` and any of the secrets are
still the development defaults, or `CORS_ORIGINS` is `*`.

## Not in this pass

Deliberately scoped out, each additive: Stripe billing and plan limits, alert
rules and notifications, the staff/platform console, the reverse direction
(provisioning Odoo employees into BioTime), and a second provider. The seams for
all of them are in place — `Capability`, the provider registry, the audit log and
the role model.
