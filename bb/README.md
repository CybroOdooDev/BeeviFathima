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

`create_all` only adds missing tables. It never alters an existing one, so a
model that gains a **column** has that column simply absent — and the first query
mentioning it fails with `no such column`, mid-request or mid-sync. So after
pulling a new version:

```bash
python3 tools/migrate.py            # report what the database is missing
python3 tools/migrate.py --apply    # add it
```

That handles additive changes — new tables, nullable columns, missing indexes —
and one that is not additive: relaxing a `NOT NULL` the model has since made
optional. PostgreSQL takes an `ALTER`; SQLite has no such statement, so the
table is rebuilt (create, copy, drop, rename) inside a transaction, and the
database file is copied first because that is the one step here that could lose
a row. Renames, drops and retypes are still Alembic's job.

**Skip it and login breaks.** `select(User)` names every column the model
declares, so one missing column makes every login fail before the password is
even checked. The app warns at boot, and a request that hits the gap answers
**503** naming the fix rather than a bare 500 — because from a login form a 500
reads as "wrong password" and sends you hunting in the wrong place.

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
The schedule runs inside this process, so syncs start happening on their own as
soon as both sides are connected — see [Running on a
schedule](#running-on-a-schedule). Triggered syncs run **inline** rather than
queueing into a void, so a deployment with no Celery worker still works; it just
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
python -m pytest              # 141 tests, no skips
```

If it reports **skips**, a dev dependency is missing. Without `pytest-asyncio`
pytest skips every async test and still exits 0 — a green run with the entire
scheduling loop untested. `tests/conftest.py` turns that into a hard failure
naming the fix, because a missing dependency must not be able to hide a broken
scheduler.

Coverage is aimed at what actually breaks in the field: pairing across sync
cycles, timezone conversion in four zones, ledger idempotency, cursor behaviour
when a provider fails mid-fetch, tenant isolation through the real HTTP stack,
that a tenant's derived key cannot decrypt another tenant's secrets, and the
quiet scheduling failures — a schedule that never fires, one that fires twice
from two replicas, one that drifts later every run. The staff console has its
own file, and most of it tests what that console *cannot* reach.

`tools/e2e_proof.py` goes further and drives the engine against a **live Odoo**
with no stubs on either side.

## Running on a schedule

Nobody presses a button. `uvicorn app.main:app` runs the clock itself: the API
process holds a loop that wakes every `SCHEDULER_TICK_SECONDS`, asks which
accounts are past their own interval, and syncs them.

That is the whole deployment. No crontab, no broker, no worker — one supervised
service:

```bash
sudo cp deploy/biobridge.service /etc/systemd/system/
sudo systemctl enable --now biobridge        # survives crashes and reboots
```

or `docker compose -f deploy/docker-compose.yml up -d`.

**Is it actually running?** That is the question that matters, because a
BioBridge whose API answers while its scheduler is dead looks perfectly healthy
from the outside — attendance just stops appearing in Odoo, and nobody notices
until payroll. So:

```bash
curl -fsS localhost:8000/health/scheduler   # 200 when live, 503 when stalled
```

Point an uptime monitor at that, not at `/health`. The dashboard reads the same
signal and says *Next sync in 7 min* or *Automatic sync is not running* — derived
from the scheduler's heartbeat, never from the configured interval.

### Scaling it

| Situation | What to run |
| --- | --- |
| One box | Just the API. `SCHEDULER_MODE=auto` |
| Several API replicas | Still just the API — a database lease elects one dispatcher |
| Syncs on their own machines | `REDIS_URL=…` plus a Celery worker and beat |

Two API replicas do not mean two syncs. Every tick claims a lease — a single row,
claimed by a conditional `UPDATE`, atomic on SQLite and Postgres alike — and only
the holder dispatches. A process that is killed releases nothing and needs to:
the lease expires and the next tick takes over. That is the entire failover
story, with no election and no coordinator.

Celery beat claims the same lease and calls the same `services.scheduling`
functions, so the two paths cannot drift and the health signal reads identically
under either.

### The rules it follows

- **Due-ness is measured from the last run's start, not its finish.** Otherwise a
  four-minute cycle on a fifteen-minute interval drifts four minutes every run,
  and by evening "every 15 minutes" means something else.
- **A never-synced account is due immediately**, so connecting both sides and
  waiting produces data rather than a fifteen-minute silence that reads as
  broken.
- **A slow cycle is never started on top of itself.** The tenant is skipped until
  it finishes.
- **Repeated failures widen the interval ×4.** A customer's BioTime box that has
  been off for a week should not be polled every fifteen minutes forever. One
  success restores the configured interval.
- **Concurrency is bounded** (`SCHEDULER_CONCURRENCY`): fifty accounts must not
  mean fifty simultaneous connections into fifty customer LANs.
- **An account with nothing connected is skipped**, not dispatched into a failed
  run that says "nothing is connected" seconds after signup.
- **Stale open shifts are closed hourly.** One employee who forgets to badge out
  otherwise holds a record that blocks every later check-in for that person.

### Changing one customer's cadence, as staff

Each account sets its own interval in Settings. Support can set it for any
account from the **Platform → All accounts** console: a table of every customer
with their interval, next sync, last result, and whether they have been backed
off into the slow lane.

From **Platform → All accounts**, staff can:

| | |
| --- | --- |
| Scheduling | Interval, automatic sync on/off, clear the failure backoff |
| Account | Company name, status, display timezone |
| Pairing | Mode, dedupe window, max shift, shift-day boundary, orphan-out policy |
| Working hours | Day start and grace, for late scoring |
| Onboarding | Create an account and its owner, password shown once |
| Support | Run a sync now; see why an account is stuck |

Status is the off switch that does not depend on the customer: only *trialing*
and *active* sync, so suspending one stops it immediately. Changing pairing
rules does not rewrite history — existing records stand and the new rules apply
from the next run.

**Diagnostics are counts and error text, never the ledger.** Staff see how many
punches are pending, in error, unmapped or stuck at the retry cap, plus each
distinct Odoo message — with employee names scrubbed out. That last part needs
real work rather than just withholding columns, because Odoo writes the person's
name into its own error: *"Cannot create new attendance record for Sara Tanaka,
the employee was already checked in…"*. Handing that over while hiding the badge
column would leak exactly what the console claims not to show, so the text is
redacted against that tenant's known employee names and reads
`…for <employee>, the employee was already checked in…`.

**A platform user is not a tenant.** `User.tenant_id` is nullable, and staff
have none. Forcing one on them put a phantom company in the customer list,
counted it in "N accounts scheduled", and polled a BioTime server that does not
exist — while handing a support engineer a meaningless attendance dashboard of
their own. With no tenant, every tenant-scoped route answers **403** naming the
reason, the scheduler has nothing to pick up, and the dashboard shows the
console alone: no Overview, no Connections, a *platform staff* pill where the
company name would be.

A tenant is optional, not forbidden. Someone who genuinely is both a customer
and staff keeps their account and gains the console on top; `--list` says which
is which.

Reaching any of it needs `is_platform_admin`, which is **not** a role either.
Roles (owner, admin, viewer) are positions inside one customer's account and
every query filters on that account. No HTTP route sets the flag — the only way
in is on the server:

```bash
python3 tools/grant_admin.py --create --email ops@yourcompany.com   # no tenant
python3 tools/grant_admin.py --list
python3 tools/grant_admin.py --email someone@existing.com           # promote
python3 tools/grant_admin.py --email someone@existing.com --revoke
```

The privilege therefore costs exactly what a database login costs, and cannot be
escalated through the product: a compromised support account cannot promote
itself or anyone else.

Credentials are never exposed — the console shows *whether* Odoo and a device
platform are connected, never the URLs or keys — and neither is the punch
ledger. Every change is written into **that customer's own** audit trail, naming
the staff user, so it is visible to the customer rather than only to you.

One thing to know: a new interval counts from that account's **last** sync, not
from now, because due-ness is measured that way everywhere. Shortening the
interval on a customer who synced ten minutes ago makes them due immediately —
the console shows the recomputed next run so that is visible rather than
surprising.

### Proving it

```bash
python3 tools/schedule_proof.py --base http://127.0.0.1:8000 \
    --odoo-url https://your.odoo.com --odoo-db yourdb \
    --odoo-user you@example.com --odoo-key <api-key>
```

Creates an account, connects both sides, sets a one-minute interval — and then
does nothing but watch. It fails unless it sees a run tagged `schedule` land
attendance in Odoo; a `manual` run would only prove the button works. Start the
API with `SCHEDULER_TICK_SECONDS=5` so it finishes in a minute.

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
    scheduling.py   who is due, and who may dispatch. Shared by both schedulers
    scheduler.py    the loop that runs inside the API process
    connections.py  the only module that decrypts credentials
  api/           deps (tenant isolation) · auth · connections · sync
                 admin.py — the one router that crosses tenants, staff only
  workers/       Celery app, beat schedule, per-tenant Redis lock
  static/        the dashboard — plain ES modules, no build step
deploy/          systemd unit · Dockerfile · compose, with restart policies
tools/           mock BioTime server · schema init and migrate · grant_admin · proofs
                 UI smoke · ledger dump · resync · user inspector
                 ledger dump · resync
```

## The dashboard

Served from `/app/`, with `/` redirecting there. Six screens:

| Screen | What it answers |
| --- | --- |
| Overview | Is it running, did the last sync work, what needs me |
| Attendance | What did BioBridge write, and did it land in Odoo |
| Activity | Every punch ever pulled, with its state and why. Filter by device, badge and date |
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

### What did this sync fetch?

Every run on Activity links to the punches it brought in — click the punch count
on the row. The ledger then explains the arithmetic the counters hide: *read 40
punches, added 6; the other 34 were already here.* That gap is the overlap
re-read, deliberate and usually small: each run re-reads
`FETCH_OVERLAP_MINUTES` of known punches, because devices upload late and their
clocks drift.

So attribution is to the run that **first ingested** a punch, not every run that
read it. A punch is fetched many times and ingested once, and re-attributing it
on each re-read would rewrite older runs' history under you.

From a terminal:

```bash
python3 tools/show_punches.py --email you@example.com --runs   # ids and counts
python3 tools/show_punches.py --email you@example.com --run <run-id>
```

### When someone cannot log in

```bash
python3 tools/show_users.py                          # every account
python3 tools/show_users.py --check you@acme.com --password 'secret'
python3 tools/show_users.py --email you@acme.com --unlock
python3 tools/show_users.py --email you@acme.com --set-password 'new one'
```

It reads the database directly, not the API — the reason to run it is usually
that the API will not let you in, and a diagnostic that needs the thing you are
diagnosing is no diagnostic. It checks the schema first (a missing column breaks
every login), then reports each account's lockout, active flag, failure count and
last login, and `--check` verifies a password against the stored hash so "wrong
password" is separated from "something between the browser and the database is
broken". Hashes are never printed.

Login refuses for four reasons, and they answer differently: **503** with a
missing column, **429** while locked out after 8 failed attempts (15 minutes),
**403** if the account is disabled, **401** for a genuine mismatch.

### When a device platform is unreachable

A sync whose BioTime server is off now reads like this, in the run list and on
the Connections page:

```
Cannot reach BioTime at http://10.0.0.9:8090: the connection was refused —
nothing is listening on that port. Check BioTime is running, that the port in
the URL is the one it serves on, and that this host can reach it.
```

Four causes are separated, because they need opposite responses:

| What you see | What it means |
|---|---|
| connection was refused | the host is up, nothing is on that port — wrong port, or BioTime is stopped |
| hostname does not resolve | DNS; use the IP if the box has no entry |
| nothing answered within *N*s | packets are being dropped — a firewall, or a VPN that is down |
| accepted but did not reply | BioTime is up but slow; a shorter sync interval keeps each query's window small |

Punches already in the ledger are untouched — an outage delays the push, it does
not lose anything. One dead site does not stop the others: each source is
fetched independently, a failed one is marked `failed` with its own message, and
the run finishes `partial`. Only when *every* source fails does the run fail, and
that counts towards the failure streak, so a server that stays dead is eventually
badged `degraded` and moved to the slow lane rather than polled every few minutes
forever.

#### Finding out which layer is broken

"Connection refused" is where diagnosis starts. This walks the connection
outward and stops at the first thing that genuinely fails:

```bash
python3 tools/check_source.py                        # every active source
python3 tools/check_source.py --tenant acme
python3 tools/check_source.py --url http://10.0.0.9:8090 --username admin
```

```
demo-company / Primary BioTime  —  http://localhost:8090
  [  ok  ] URL parses: http://localhost:8090
  [  ok  ] DNS resolves to 127.0.0.1
  [ FAIL ] nothing accepts a connection on localhost:8090 (Connection refused)

  Broken at: tcp

  What to do:
    - The connection was refused, which means the host is up and answered
      — nothing is listening on that port.
    - Something IS listening on localhost: port(s) 8099. If BioTime moved,
      point the Server URL at the right one.
```

It reads the database directly rather than the API, because the reason to run it
is usually that something is down. It writes nothing, so it is safe against
production. Things it catches that guessing does not:

- **An open port that never answers.** TCP connecting instantly and HTTP timing
  out has three causes that look identical: a server that is merely slow, a
  non-HTTP service on the port, and a single-threaded server wedged by an
  abandoned connection. It sends a raw request on a bare socket with a longer
  budget and reports which — "it IS a web server, just slower than 14s" versus a
  banner naming what actually owns the port versus "accepted the connection,
  then sent nothing".

- **The port moved.** On a refusal it scans the ports BioTime is commonly on and
  names the live one. On a *timeout* it deliberately does not — a filter drops
  every port equally, so a scan there can only point at the wrong cause.
- **`localhost` from a container.** If BioBridge is containerised, `localhost` is
  the container, not the machine you are looking at; the port really is open and
  really is unreachable. It says so.
- **A bad password, not a bad network.** Reaching the auth layer at all proves
  the network is fine.

#### Running the mock on the right port

The mock defaults to **8099**, which is usually not what is in the connection
form — and a port mismatch fails forever while looking exactly like a dead
server. It prints the URL to paste, and binds loopback-only unless told
otherwise:

```bash
python3 tools/mock_biotime.py --port 8090 --punches punches.json
python3 tools/mock_biotime.py --host 0.0.0.0 --port 8090   # BioBridge elsewhere
```

It runs in the foreground and dies with its terminal. If a sync that was working
starts refusing connections after you close a shell or reboot, that is why —
`nohup`, `tmux`, or a systemd unit alongside `deploy/biobridge.service`.

It is threaded, and that is not incidental. It used to be a single-threaded
`HTTPServer`, where one client that opened a connection and never completed a
request blocked every subsequent one *forever* — and a browser tab left open on
it is enough to do that. The kernel keeps accepting into the backlog, so the
port still passes a TCP check while nothing is ever served: a sync that reads as
a hung BioTime, on a mock that looks fine. Each connection now gets its own
thread and goes quiet after 10 idle seconds.

#### Proving the error path itself

```bash
python3 tools/outage_proof.py
```

Runs a real cycle through the real provider against a dead port, and prints the
three places the failure surfaces — the run row, the connection status, and the
streak.

### Looking at the punch records

Activity holds the ledger: every punch ever pulled, with the time the device
reported, the same instant in UTC, the direction BioBridge decided on, the Odoo
record it landed in, and the error if it did not. Filter by terminal, badge and
date range; the filters live in the URL, so a filtered view is a link you can
paste into a ticket.

The same thing from a terminal, when the answer belongs in a mail or a
spreadsheet rather than a browser:

```bash
python3 tools/show_punches.py --base http://127.0.0.1:8000 \
    --email you@example.com --badge 1002 --from 2026-09-13 --to 2026-09-15
python3 tools/show_punches.py --email you@example.com \
    --device GATE-01 --state error --csv errors.csv
```

It groups by terminal, prints device-local and UTC side by side — nearly every
"the times are wrong" report is visible right there as a constant offset — and
repeats each distinct error in full rather than truncated into a column.

### Re-pushing after you fix something in Odoo

"Delete the attendance in Odoo and sync again" **re-creates nothing**, and it is
worth knowing why before trying it on a live database. Three separate mechanisms
stand in the way:

- A punch that reached Odoo is marked `synced`, and the push step never looks at
  it again. That is what stops every sync rewriting the whole history — so the
  run after your deletion reports success having done nothing.
- A punch that has failed five times is dropped from the push entirely
  (`attempts < 5`). Past that the run reports **success with zero errors** while
  those punches sit in `error` indefinitely. Clearing the Odoo record does not
  revive them; only resetting the counter does.
- Attendance is mirrored locally against the Odoo record id, so a shift
  re-created under a new id shows up **twice** on the Attendance screen — once
  for the dead id, once for the live one.

So the recovery is: delete only the records actually in the way, put the affected
punches back in the queue, then sync.

```bash
python3 tools/resync.py --base http://127.0.0.1:8000 --email you@example.com
# ^ report: what is stuck, the attempt counts, and each distinct Odoo message

python3 tools/resync.py --email you@example.com --reset-errors --sync
```

`--reset-synced` re-pushes punches that already landed, for when you have
deliberately removed their Odoo records — Odoo refuses them as overlaps if you
have not. `--prune` lists the shifts mirrored more than once. Nothing is
destructive without a flag and a typed confirmation.

`tools/ui_smoke.py` drives it in a real browser: sign up, connect both sides,
import terminals, run a sync, read every screen, filter the ledger, and fail on
any console error.

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
| `REDIS_URL` | *(empty)* | Empty means no broker: syncs run inline, and the API schedules |
| `SCHEDULER_MODE` | `auto` | `auto` · `inprocess` · `celery` · `off` |
| `SCHEDULER_TICK_SECONDS` | 60 | How often the loop wakes, not the sync frequency |
| `SCHEDULER_CONCURRENCY` | 4 | Accounts synced at once |
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
