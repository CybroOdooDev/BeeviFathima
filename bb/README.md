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
uvicorn app.main:app --reload --reload-dir app   # dashboard on http://localhost:8000/
```

`--reload-dir app` matters, not just tidiness: without it, the reloader watches the
whole project root, `.venv` included, and `.venv` holds every dependency's entire
source tree — tens of thousands of files. That reliably blows past the OS's inotify
watch limit on a fresh machine or container, and uvicorn fails before it ever binds
the port. See the note below if you hit `OSError: OS file watch limit reached`
anyway — the app's own code under `app/` is all `--reload` ever needs to watch.

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
uvicorn app.main:app --reload --reload-dir app --port 8001
```

If instead the traceback ends in `OSError: OS file watch limit reached` naming a
path under `.venv` (Celery is a common one — it alone ships thousands of files),
`--reload-dir app` above is the fix, not a bigger limit: it stops the reloader
recursing into every installed dependency and watches only the code that
actually changes. Raising the limit instead works too, and needs no code change:

```bash
sudo sysctl fs.inotify.max_user_watches=524288    # this boot only
```

But that is treating the symptom — the reloader is still doing thousands of
times more work than watching `app/` alone does, on every machine this ever
runs on.

SQLite and no broker out of the box, so there is no database or queue to set up.
The schedule runs inside this process, so syncs start happening on their own as
soon as both sides are connected — see [Running on a
schedule](#running-on-a-schedule). Triggered syncs run **inline** rather than
queueing into a void, so a deployment with no Celery worker still works; it just
does the sync inside the request.

Try the whole loop with no hardware:

```bash
python3 tools/generate_punches.py                       # writes punches.json, timed off right now
python3 tools/mock_biotime.py --punches punches.json     # fake BioTime on :8099
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
| Onboarding | Create an account and its owner, password shown once, plan optional |
| Subscription | Stop and restart syncing with a reason; assign a plan; set the renewal date — see [Subscription plans](#subscription-plans) |
| Support | Run a sync now; see why an account is stuck |

Status is the off switch that does not depend on the customer: only *trialing*
and *active* sync, so suspending one stops it immediately. Changing pairing
rules does not rewrite history — existing records stand and the new rules apply
from the next run.

#### Stopping an account for non-payment

Each row in the console has **Deactivate**, and a deactivated one has
**Activate**. Two clicks to stop (the second is where an optional reason gets
typed), one to restart. No dialog: nothing in this app uses a modal, and a
`confirm()` blocks the whole page.

```
POST /api/v1/admin/tenants/{id}/deactivate   {"reason": "unpaid invoice 4021"}
POST /api/v1/admin/tenants/{id}/activate
```

Its own action rather than the Status dropdown in the form below, because this
is the one taken when a subscription lapses: it deserves one click from the
list, a reason attached, and one clear line in the audit trail instead of
`status: active -> suspended` among a form's other changes.

**It changes `status`, never `sync_enabled`.** Those are different questions and
conflating them is the trap: `sync_enabled` is the customer's own switch, so a
suspension built on it is one the customer simply turns back on. Keeping them
apart also means reactivating hands back the setting they chose rather than
switching syncing on for an account that had it off on purpose.

**Nothing is destroyed and nothing is hidden.** The customer keeps their
records, their dashboard and every screen; only the collection of new punches
stops. That is what makes this safe to use on a billing decision that might be
wrong — and it is why the gate is a status change rather than anything that
touches data.

Deactivating closes all three doors, which it did not always:

| | Before | Now |
| --- | --- | --- |
| The scheduler | skipped the account | skips it |
| The customer's **Sync now** | **ran the sync** | refused, 403 with the reason |
| The console's **Sync now** | refused | refused |
| `SyncEngine.run_cycle` | ran | refuses, and records a run saying why |

The middle row was the hole: suspension stopped the clock without stopping the
syncing, so an account stopped for non-payment kept syncing for as long as
somebody kept pressing the button. The engine now checks it too, so a route
added later that forgets still cannot sync a stopped account — there is exactly
one place a cycle can start. It refuses as a `SyncAborted`, which records the
run and its reason but is exempt from the failure streak: a stopped account is
not a failing one and must not be slow-laned or badged degraded for it.

**The customer is told.** They get a banner on the overview and in Settings, the
sidebar pill reads *sync stopped*, and the schedule card says the account is
stopped. That last one was actively misleading before: `next_run_at` is null for
a suspended account, so the card fell through to "this account has sync turned
off in Settings" and sent people to a screen where their own switch was plainly
still on.

**The reason is a staff note and stays one.** It is returned only to the
console, never on `/tenant`, and it is deliberately kept out of the audit
`detail` as well — that trail belongs to the customer and exists to be shown to
them, so "chasing payment, third email" must not be sitting in it the first time
someone adds that screen. It lives on the tenant row and in the server log.

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
and staff keeps their account — they just hold one hat at a time, which is what
the two doors below are for; `--list` says who is which.

#### Two doors, two kinds of session

Staff sign in at **`/#/staff/login`**, customers at `/#/login`, and the token a
door mints is scoped to that surface:

| | Customer door | Console door |
| --- | --- | --- |
| Endpoint | `POST /auth/login` | `POST /auth/staff/login` |
| Token scope | `tenant` | `staff` |
| Reaches `/admin/*` | never | yes |
| Reaches tenant routes | yes | never |
| Access token life | 12 h | 1 h |
| Refresh token life | 30 d | 1 d |

**The scope comes from the door, not from the account.** Before the split, the
flag on the user row decided what the console answered, so a support engineer
signing in to look at their own attendance was handed a session that could also
list every other customer. Now that same sign-in is tenant-scoped and the
console refuses it — they have to go through the console door, which is also
where the short session life applies. A refresh returns to the surface it
started on, so a customer session can never renew itself into a console one.

Two consequences worth knowing:

- **The flag alone no longer opens the console.** After `grant_admin.py`, the
  person must sign in *at the staff door*; an existing session will not do. If
  they also have their own workspace, the sidebar there links across.
- **A staff account with no workspace is turned away from the customer door**,
  with the address of the right one, rather than being given a session that
  authenticates and then fails on every screen.

The console door answers a non-staff account exactly as it answers a wrong
password. Saying "you are not staff" would turn it into a lookup for which
accounts hold the flag — the shortlist most worth phishing — so the refusal is
logged server-side instead. Rate limiting and lockout are shared with the
customer door, so the console is not the softer target.

None of this is what keeps anyone out: `get_platform_admin` and `get_principal`
check every request server-side, and they are still the control. What the split
buys is that a console credential is never typed into the customer form, that a
cross-tenant token expires in an hour rather than twelve, and that
`user_session.scope` can answer "which live sessions could reach other
customers" during an incident — a question `tenant_id` cannot answer, because a
dual-role person has a tenant either way.

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

Two of these have their own proof, and neither needs Odoo or BioTime: each boots
the app against a throwaway database and drives a real browser, because what
they check are rendering decisions that fail silently.

```bash
python3 tools/login_doors_proof.py    # the two sign-in doors
python3 tools/gate_proof.py           # stopping and restarting an account
```

`login_doors_proof.py` asserts that a customer is never shown the console, that
a staff-only account is refused at the customer door and admitted at the other,
that signing out of the console returns to the *console* login rather than the
customer one, and that a dual-role user's customer session shows no console nav
but does offer a link across to it.

`gate_proof.py` stops an account from the console and then checks what the
*customer* sees — the banner, the pill, the schedule card, the refused Sync now
button, the untouched switch in their Settings — and that the staff reason never
appears on their screen. Then it restarts the account and checks they are back
to normal.

`tests/test_login_separation.py` and `tests/test_subscription_gate.py` cover the
API side of each, including that a customer-door token is refused by `/admin/*`
even when the account is staff, and that a suspended customer's own Sync now is
refused.

```bash
python3 tools/schedule_proof.py --base http://127.0.0.1:8000 \
    --odoo-url https://your.odoo.com --odoo-db yourdb \
    --odoo-user you@example.com --odoo-key <api-key>
```

Creates an account, connects both sides, sets a one-minute interval — and then
does nothing but watch. It fails unless it sees a run tagged `schedule` land
attendance in Odoo; a `manual` run would only prove the button works. Start the
API with `SCHEDULER_TICK_SECONDS=5` so it finishes in a minute.

## Subscription plans

A plan is a tier with enforced limits, not a billing record. Nothing in this
codebase charges a card — the platform's own choice is to drive syncing off an
internal "paid through" date on the tenant (`subscription_renews_at`), and let
staff move that date by whatever process they already use to get paid. Two
limits are enforced today:

| Limit | Enforced | Where |
| --- | --- | --- |
| `min_sync_interval_minutes` | On the **customer's own** settings save | `PATCH /api/v1/tenant` |
| `max_employees` | On **new** badge-to-employee matches during a sync | `SyncEngine._resolve_mappings` |

**Staff are never blocked by either.** The interval floor is a limit on
self-service, not on the platform's own ability to make an exception — the
console can set a tighter interval than a plan allows, the same way it can
already change status or pairing rules directly. The employee cap only ever
holds back a *new* match; a plan assigned or lowered after a tenant already
has more mapped employees than its new cap allows does not unmap anyone —
the badges already relying on it keep working, and only the next new one is
held back, with a note explaining why instead of "no such employee".

Manage plans with:

```bash
python3 tools/seed_plans.py     # create/update the starter Starter/Growth/Scale tiers
```

Safe to re-run — it upserts by name and never deletes a plan, because a
tenant already on one must keep existing regardless of what the script's
defaults currently say. Plans are not created through the API on purpose:
deciding what to sell is a business decision, not a customer- or
staff-reachable action. The console only *assigns* one (`plan_id` on
`PATCH /api/v1/admin/tenants/{id}/config`) or lists what exists
(`GET /api/v1/admin/plans`). Whichever plan has `is_default` set is what a
self-signup and a staff-created account get when nothing else is specified.

### Choosing a plan yourself

A tenant is never stuck with whatever plan they were assigned. Two places:

- **Signup** — `GET /api/v1/auth/plans` (public, no token needed) lists the
  active plans. Two different questions, not one dropdown: `plan_id` picks
  which plan (left unset, falls back to whichever is `is_default`), and
  `skip_trial` decides whether there is a trial at all.
  - `skip_trial: false` (the default) — status opens `trialing` for
    `TRIAL_DAYS`, whatever `plan_id` says. Picking a plan here just commits
    the trial to that plan's limits instead of the default's.
  - `skip_trial: true` — status opens `active` immediately, for
    `BILLING_PERIOD_DAYS` instead of a trial, and `plan_id` is then
    required: "no trial, no chosen plan" is not a request this endpoint can
    act on. There is still no billing integration behind this — nothing
    processes a payment — `active` and a billing-cycle-length date are just
    the bookkeeping for "treat this as already paid for", the same
    fictional-but-useful stand-in `subscription_renews_at` already is
    everywhere else in this feature.
- **Settings, afterward** — `PATCH /api/v1/tenant` takes the same `plan_id`.
  Repeatable, any time, no confirmation step beyond the one click for an
  account not yet on a paid plan — see below for what changes once one is.

Both are self-service; staff's own `plan_id` on
`PATCH /api/v1/admin/tenants/{id}/config` still exists, unchanged, for
onboarding a customer directly or overriding their choice (and always
applies immediately — see "Deferred switching" below). One thing a customer
cannot do that staff can: clear their own plan back to none.
`TenantUpdate.plan_id` refuses `null` — an active plan or nothing happens —
because going unenforced is not a self-service action.

A switch that takes effect (immediately or once deferred — next section —
lands) is judged the same way a plan already assigned is: the employee cap
only ever holds back a *new* match (see above — nothing here needed to
change for that), and the interval floor is enforced the moment it lands. If
the account's `sync_interval_minutes` is then under the new plan's floor, it
is raised automatically rather than left holding a setting the plan it just
joined would reject — the same fix already applied to a fresh signup, now
applied everywhere a switch could reintroduce it.

#### Deferred switching: not mid-period

Self-service switching applies immediately **unless the account is already
on a paid plan** — `status == active` and a `plan_id` already assigned, i.e.
something has actually been paid for. In that case the new choice is queued
in `Tenant.pending_plan_id` instead of landing on the spot, and only takes
effect once the *current* plan's `subscription_renews_at` is reached — the
same sweep that moves the renewal date (below) promotes it. A `trialing`
account has paid for nothing yet, and a `past_due` one has already lapsed,
so either still switches on the spot, exactly as if this did not exist.

Choosing the plan already in effect while a switch is queued cancels it
(`pending_plan_id` back to null) — that is the only "undo" this needs, since
there is nothing else to roll back. Settings shows the pending choice and
when it lands (`GET /api/v1/tenant`'s `pending_plan_id` / `pending_plan_name`
— also on the staff console's tenant rows) so it is never a silent queue.
Staff setting `plan_id` directly always supersedes and clears any pending
switch — their override outranks a customer's still-queued one.

### The automatic renewal-date check

A scheduled sweep — `app.services.scheduling.sweep_subscriptions` — moves
accounts across `subscription_renews_at` the same way staff already move them
by hand:

- `trialing` / `active` past its renewal date → `past_due`. **No grace
  period**: the moment the date passes, syncing stops, exactly like a manual
  deactivate. `past_due` was already excluded from `SYNCABLE_STATUSES`, so
  the gate itself needed no change — this only automates *reaching* it.
- `past_due` whose date has since moved into the future → `active`. Staff (or
  a future billing integration) pushes the date forward; the next sweep
  notices and lets the account back in.
- `suspended` and `cancelled` are **never** touched, in either direction —
  those are deliberate acts and must outlast the sweep, or the deactivate
  button would become a temporary measure instead of the one it is documented
  to be.

**A null `subscription_renews_at` is never touched either — the single most
important rule in this feature.** That is the state of every tenant that
existed before this column did. If the sweep treated "no date set" as
"already lapsed", turning it on would suspend every existing customer the
first time it ran. An account only ever lapses because a real date passed.

The same pass also promotes a queued plan switch (`pending_plan_id` — see
"Deferred switching" above) the moment `subscription_renews_at` is reached,
independently of whichever direction above the account also moves in at that
same tick — including a tenant that is already `past_due` and stays that
way, so a switch queued before a lapse is not lost by it. A tenant kept
permanently `active` by staff extending the date *before* it is ever reached
is the one case this cannot promote early: with nothing here to notice an
extension that lands ahead of the deadline, the switch simply waits for
whatever date is current when it is finally reached, later if the date keeps
moving. Reactive renewals — staff renewing after noticing `past_due`, which
is how this hand-operated system is actually used today — land the switch
exactly on the original date, since the sweep already caught that moment
before anyone intervened.

It runs on its own hourly slot, in whichever scheduler is active —
`app.services.scheduler.Scheduler._run_subscription_sweep` in-process, or the
`sweep-subscriptions` beat task under Celery — sharing the exact same
function either way, the same guarantee the due-tenant sweep already makes.

### The warning, instead of a grace period

The literal ask this answers: a warning for subscriptions ending shortly, not
a delay after they do. Once `subscription_renews_at` is within
`SUBSCRIPTION_WARNING_DAYS` (default 7) and the account is still `trialing`
or `active`, `renewal_warning` appears on `GET /api/v1/dashboard` (rendered as
a banner on the customer's Overview page, and inline on the Settings "Plan"
card) and on the staff console's `GET /api/v1/admin/tenants[/​{id}]` (a
"renewing soon" tag on the row). It is `null` once an account has actually
lapsed — the louder "stopped syncing" messaging already covers that case, and
showing both would bury the one that matters.

Inside `SUBSCRIPTION_URGENT_DAYS` (default 3) the *same* warning carries
`urgent: true` instead of becoming a second message — the customer's Overview
banner and the Settings Plan card render it in the "bad" tone instead of
"warn" at that point, and the console's row tag reads "renewing very soon".
One `trialing` or `active` account is judged by one date either way, so a
trial ending in 2 days and a paid plan ending in 2 days read identically.

### Configuration

| Variable | Default | |
| --- | --- | --- |
| `TRIAL_DAYS` | `10` | Initial `subscription_renews_at` for a trial signup |
| `BILLING_PERIOD_DAYS` | `30` | Initial `subscription_renews_at` for a signup that skips the trial (`SignupRequest.skip_trial`) — staff onboarding still always starts a trial, on `TRIAL_DAYS`, whichever plan it assigns |
| `SUBSCRIPTION_WARNING_DAYS` | `7` | How close to the renewal date before the warning appears |

## How a sync run works

```
fetch → ingest → normalise → register → (provision) → map → pair → push → record
```

`provision` is opt-in per source — see "Employee mapping" below.

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

## Employee mapping

Odoo is the roster of record. A punch's badge (`emp_code`) is matched to an
`hr.employee` by trying `barcode` → `pin` → `registration_number` →
`work_email`, in that order, against whichever of those fields actually
exist on the customer's Odoo (`OdooClient.find_employee` /
`MATCH_FIELDS`). A match is cached on `EmployeeMapping`; ambiguous matches
(two employees sharing the same field value) and unmatched badges are
surfaced for a human to resolve by hand, or, if `tenant.auto_create_employees`
is on, an unmatched badge creates a new `hr.employee`.

That direction — device badge to Odoo — always assumed the person already
existed on the device. Going the other way — Odoo employee to device — is
`DeviceSource.auto_provision_employees`, off by default per source. When
on, `SyncEngine._provision_employees` runs once per cycle, ahead of
mapping and independent of the punch stream: it reads Odoo's active
roster, picks each employee's matching identifier with the same
`MATCH_FIELDS` priority (`OdooClient.employee_code_for`, the mirror of
`find_employee`), and calls the provider's `create_employee` for anyone
missing there. The matching key never changes per vendor — only the create
call does, which is why enabling this for BioTime and ZKTeco needed no
change to the matching logic itself, just each provider implementing
`fetch_employees`/`create_employee` (`Capability.READ_EMPLOYEES` /
`WRITE_EMPLOYEES`).

**This provisions identity only, never a biometric template.** No vendor's
protocol lets a fingerprint or face be pushed to a device remotely —
BioTime's REST API doesn't expose it and neither does ZKTeco's wire
protocol. `create_employee` creates the record (id/name, and for ZKTeco a
device `uid` slot) a person can then clock against once someone enrolls
their fingerprint or face locally at the terminal, or issues a card/PIN.
"Seamless" here means the roster entry is waiting for them before their
first day, not that enrollment itself is remote — that step needs a human
at the physical device, for every vendor.

ZKTeco standalone terminals have one additional constraint worth knowing:
the attendance-log wire record truncates a user id to 9 bytes, while the
live user table allows 24. `ZKDeviceProvider.create_employee` refuses to
provision an `emp_code` longer than 9 characters, because a longer one
would enroll fine and then silently fail to match every future punch back
to that person.

## Multi-company Odoo isolation

A customer's own `tenant_id` isolates two BioBridge tenants from each
other in BioBridge's own database, always, regardless of what's on the
Odoo side. That says nothing about a customer running Odoo's *own*
multi-company feature — several companies inside one Odoo instance, each
with its own employees, its own device platform, sold to BioBridge as
what looks like two unrelated tenants but is actually one Odoo database
underneath. Left alone, `OdooClient` has no idea that distinction exists:
every `search_read` it issues is scoped only by whatever companies the
authenticated Odoo API user happens to be a member of, which for a shared
integration user is often "all of them."

`OdooConnection.company_id` closes that gap — the res.company id this one
connection is pinned to, set from the company list Test Connection returns
(`OdooClient.list_companies`, and `ping()` refuses to proceed if the
configured id isn't actually visible to that login). Once set,
`OdooClient.execute` puts it in `allowed_company_ids` on *every* call, not
just the ones this file happens to add a domain filter to — that context
is what makes Odoo's own multi-company record rules apply, and it holds
even for a call with no domain at all (`close_attendance` writes by id).
`find_employee`, `list_employees`, `create_employee` and the attendance
lookups add an explicit `company_id` domain condition too, as
defense-in-depth alongside the context, guarded by `fields_of` the same
way every other optional field in this client is.

One exception needed its own fix rather than inheriting the guarantee for
free: `x_biobridge_device` (bootstrap-mode device tracking, `## Employee
mapping` above's sibling — see `ensure_device_tracking_bootstrap`) is a
plain custom model BioBridge creates over the API, with no multi-company
rule of its own the way the real add-on's `biobridge.device` has. Company
scoping there is a genuine, load-bearing field (`x_company_id`), added
during bootstrap and backfilled automatically the next time an existing
bootstrap connection re-runs "Set up device tracking" (bootstrap stopped
being a no-op once device tracking already exists — see the docstring).

Leaving `company_id` unset is still the right choice for an ordinary
single-company Odoo — there's nothing to isolate from, and every existing
connection predates this field and keeps working exactly as before.

## Layout

```
app/
  core/          config · Fernet envelope encryption · JWT and password hashing
  db/            declarative base, UUID and timestamp mixins, session factory
  models/        tenant · users · sessions · connections · devices · ledger
  integrations/
    base.py      the seam: AttendanceProvider, PunchEvent, Capability, registry
    providers/   biotime, zkteco (standalone terminals) — add a vendor here,
                 nothing above changes
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
server-timezone field warns that a wrong value shifts every attendance by hours.

Every timezone field (a source's server/device timezone, a tenant's own display
timezone, both places it's set during signup or onboarding) is a plain text
input with an `<input list>`/`<datalist>` of every IANA zone name
(`Intl.supportedValuesOf('timeZone')`, "UTC" added back in since that API's own
list omits it) for autocomplete-as-you-type — still free text, not a strict
`<select>`, so an older browser without that API just loses the suggestions,
not the field. What actually stops a bad value is the server: every schema with
a timezone field (`app/schemas.py`'s `_validate_timezone`) rejects anything that
isn't a real zone `zoneinfo` recognizes, so a typo is a 422 at save time instead
of a silent fall-back to UTC three steps later in `app/services/timeutils.py`.

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

Login refuses for five reasons, and they answer differently: **503** with a
missing column, **429** while locked out after 8 failed attempts (15 minutes),
**403** if the account is disabled, **403** at the customer door for a staff
account with no workspace of its own (see [two doors](#two-doors-two-kinds-of-session)),
**401** for a genuine mismatch.

One to watch for on the console door: a staff member who mistypes their email
gets the same **401** as someone whose account simply is not staff, because the
door deliberately does not distinguish the two. If a real staff sign-in is
refused, check `--list` before suspecting the password — and the server log,
which records the non-staff refusal by name.

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

#### Generating the punches it serves

```bash
python3 tools/generate_punches.py
python3 tools/generate_punches.py --days 10 --emp-codes 1001,0042,A7,9001
python3 tools/generate_punches.py --tz Asia/Kolkata --check-in 08:30 --check-out 17:30
```

Writes a `punches.json` timed **relative to right now**, not to fixed clock
values — which matters more than it sounds like it should. A punch hand-typed
for 17:00 today, written at 15:00, sits in the future relative to any sync that
runs before 17:00, and BioTime's own fetch window silently drops anything past
"now" (`app/services/sync_engine.py`'s `end_utc` is capped at `utcnow + 5
minutes` of clock-skew tolerance). The punch is simply never fetched — no error,
no log line — and it reads exactly like a pairing bug instead of a clock
problem. That is how "attendance records were matched only for check-ins"
happens: the check-in was in the past, the check-out was still in the future
when the sync ran.

Re-run it whenever the file feels stale rather than keeping one around; that is
the intended use, not a one-time fixture. Defaults to the three badges
`tools/mock_biotime.py` already knows by name (Ahmed Sharma, Sara Tanaka, Jane
Haddad) across the last 5 weekdays, with today's shift left open — check-in
written, no check-out — whenever the current time is still before the
configured check-out. That is deliberate: an in-progress shift is a real state
worth having in the fixture, and it is a genuine test of the pairing engine's
open-shift handling rather than an edge case to avoid.

**`--tz` must match the *device source's* configured server timezone in
BioBridge** (Connections screen), not your own machine's — `punch_time` is a
naive local string, interpreted in whatever zone the source is set to. Getting
this wrong does not error, it just shifts every punch by the difference and can
push some of them outside the fetch window, which again looks like a pairing
bug rather than a mismatched setting.

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
rules and notifications, and the staff/platform console. The seams for all of
them are in place — the audit log and the role model.

Two items formerly listed here are done: a second provider (ZKTeco standalone
terminals — see `app/integrations/providers/zkteco.py`) and the reverse
mapping direction, provisioning Odoo's roster onto a provider — see
"Employee mapping" below.
