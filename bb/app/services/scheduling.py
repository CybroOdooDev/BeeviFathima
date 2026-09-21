"""Who is due, and who is allowed to dispatch.

Both schedulers — the loop inside the API process and Celery beat — call into
here. That is the point of the module: "is this tenant due?" is the kind of rule
that gets quietly reimplemented in the second place it is needed and then drifts,
so a customer's interval means one thing under Celery and another without it.

Nothing here touches Odoo or BioTime. It answers two questions against the
database: *may I dispatch* (the lease) and *who is due* (the interval maths).
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    LEASE_ID,
    SYNCABLE_STATUSES,
    DeviceSource,
    SchedulerLease,
    SubscriptionPlan,
    SyncRun,
    Tenant,
    TenantStatus,
)
from app.services.timeutils import ensure_aware, utcnow_naive

log = logging.getLogger(__name__)

#: Statuses that are allowed to sync. A suspended or cancelled tenant keeps its
#: data and its screens, and stops costing anybody polling traffic.
#:
#: Re-exported from the models, where it has to live so ``Tenant.syncable`` can
#: use it without importing a service. This name stays because the scheduler,
#: the console and the tests all already import it from here.
SYNCABLE: frozenset[str] = SYNCABLE_STATUSES

#: A tenant whose connections keep failing is polled this many times less
#: often. A customer's BioTime box that has been off for a week should not be
#: hit every 15 minutes forever, and their Odoo should not collect the errors.
SLOW_LANE_MULTIPLIER = 4


def owner_id() -> str:
    """host:pid — enough to tell two replicas apart in the UI and the log."""
    return f"{socket.gethostname()}:{os.getpid()}"


# ===========================================================================
# The lease
# ===========================================================================
def _ensure_row(db: Session) -> None:
    """Create the singleton row if this is a fresh database.

    Two processes starting together will both try. The loser catches the
    integrity error and carries on — by then the row exists, which is all it
    wanted.
    """
    if db.get(SchedulerLease, LEASE_ID) is not None:
        return
    try:
        db.add(SchedulerLease(id=LEASE_ID))
        db.commit()
    except IntegrityError:
        db.rollback()


def claim_lease(db: Session, *, owner: str, mode: str, ttl_seconds: int) -> bool:
    """Try to become the dispatcher for the next ``ttl_seconds``.

    A single conditional UPDATE, so the database decides the winner and no
    read-then-write race is possible: the row is claimable only when it is
    already ours (a renewal) or when the previous holder's lease has expired.

    Expiry *is* the failover. A process that is SIGKILLed releases nothing, and
    nothing needs it to — the next tick past ``lease_expires_at`` takes over.
    Hence a TTL comfortably longer than the tick interval but far shorter than a
    human would take to notice an outage.
    """
    _ensure_row(db)
    now = utcnow_naive()

    result = db.execute(
        update(SchedulerLease)
        .where(
            SchedulerLease.id == LEASE_ID,
            (SchedulerLease.owner == owner)
            | (SchedulerLease.lease_expires_at.is_(None))
            | (SchedulerLease.lease_expires_at < now),
        )
        .values(
            owner=owner,
            mode=mode,
            last_tick_at=now,
            lease_expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    db.commit()
    return bool(result.rowcount)


def release_lease(db: Session, *, owner: str) -> None:
    """Hand the lease back on a clean shutdown.

    Only a courtesy — expiry covers the unclean case. It matters during a rolling
    deploy, where the replacement process would otherwise wait out the full TTL
    before anything is dispatched.
    """
    db.execute(
        update(SchedulerLease)
        .where(SchedulerLease.id == LEASE_ID, SchedulerLease.owner == owner)
        .values(lease_expires_at=utcnow_naive())
    )
    db.commit()


def lease_state(db: Session) -> SchedulerLease | None:
    return db.get(SchedulerLease, LEASE_ID)


def scheduler_health(db: Session, *, stale_after_seconds: int | None = None) -> dict:
    """What the dashboard shows about the schedule itself.

    ``running`` is derived from the heartbeat, not from configuration. A UI that
    reports the configured interval tells the customer what *should* happen; this
    tells them what is happening, which is the only version worth showing after
    a worker has died.
    """
    stale_after = stale_after_seconds or settings.scheduler_tick_seconds * 3
    row = lease_state(db)
    if row is None or row.last_tick_at is None:
        return {
            "running": False,
            "mode": None,
            "owner": None,
            "last_tick_at": None,
            "seconds_since_tick": None,
        }

    age = (utcnow_naive() - row.last_tick_at).total_seconds()
    return {
        "running": age <= stale_after,
        "mode": row.mode,
        "owner": row.owner,
        "last_tick_at": row.last_tick_at,
        "seconds_since_tick": int(age),
    }


# ===========================================================================
# Who is due
# ===========================================================================
def effective_interval(tenant: Tenant) -> int:
    """The tenant's interval, widened while its connections keep failing."""
    interval = max(1, tenant.sync_interval_minutes)
    if tenant.consecutive_failures >= settings.max_consecutive_failures:
        interval *= SLOW_LANE_MULTIPLIER
    return interval


def last_run_started_at(db: Session, tenant_id: str) -> datetime | None:
    """The most recent run's start, always UTC-aware.

    ``SyncRun.started_at`` is ``DateTime(timezone=True)``, which comes back aware
    on PostgreSQL and naive on SQLite. Normalising here means the callers below
    never compare a naive value to an aware one — the bug that raises on one
    backend and passes on the other.
    """
    return ensure_aware(
        db.scalars(
            select(SyncRun.started_at)
            .where(SyncRun.tenant_id == tenant_id)
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        ).first()
    )


def next_run_at(
    db: Session, tenant: Tenant, *, now: datetime | None = None
) -> datetime | None:
    """When this tenant is next eligible, UTC-aware. ``None`` when off.

    Read by the UI as well as the loop, so a customer can see the answer instead
    of inferring it from an interval and a last-run time.

    ``now`` is threaded through rather than read from the clock here, because a
    never-synced tenant is due *at* the caller's ``now``. Reading the clock twice
    puts this value microseconds into the future, ``due_at <= now`` is then false
    for every tick, and a freshly connected account never syncs at all — a
    difference of microseconds that reads to the customer as a dead product.
    """
    if not tenant.sync_enabled or tenant.status not in SYNCABLE:
        return None
    last = last_run_started_at(db, tenant.id)
    if last is None:
        return now or datetime.now(timezone.utc)
    return last + timedelta(minutes=effective_interval(tenant))


def is_due(db: Session, tenant: Tenant, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    due_at = next_run_at(db, tenant, now=now)
    return due_at is not None and due_at <= now


def has_active_source(db: Session, tenant_id: str) -> bool:
    """Is there anything to poll yet?

    A tenant with no device platform connected is not failing, it is
    unconfigured — and dispatching a cycle for it writes a **failed** run whose
    only message is "nothing is connected". Which lands within seconds of
    signup, so the first thing a new customer sees on their own dashboard is
    *Last sync: failed*, before they have done anything at all.

    Odoo is deliberately not part of this gate. The engine captures punches
    without it on purpose — device platforms prune old transactions, so a punch
    not captured now may be gone — and pushes them once Odoo is reachable.
    """
    return bool(
        db.scalar(
            select(DeviceSource.id)
            .where(
                DeviceSource.tenant_id == tenant_id,
                DeviceSource.is_active.is_(True),
            )
            .limit(1)
        )
    )


def due_tenants(db: Session, *, now: datetime | None = None) -> list[Tenant]:
    """Every tenant whose interval has elapsed and that has something to poll.

    Due-ness is measured from the last run's *start*, not its finish, so a slow
    cycle does not push the schedule later and later with each run. Overlap is
    prevented by the caller (an in-flight set here, a Redis lock under Celery)
    rather than by delaying the clock.
    """
    now = now or datetime.now(timezone.utc)
    tenants = db.scalars(
        select(Tenant).where(Tenant.sync_enabled.is_(True), Tenant.status.in_(SYNCABLE))
    ).all()
    return [
        t for t in tenants if is_due(db, t, now=now) and has_active_source(db, t.id)
    ]


# ===========================================================================
# Subscriptions: the same clock, applied to whether an account may sync at all
# ===========================================================================
def sweep_subscriptions(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Move accounts across the renewal date, the way staff already do by hand.

    Two directions, both keyed on ``Tenant.subscription_renews_at`` and both
    leaving a null date alone:

    * ``trialing``/``active`` past its renewal date -> ``past_due``. No grace
      period: this is the same state a staff member sets by hand today via
      ``platform.tenant.deactivate``-adjacent tooling, and ``SYNCABLE_STATUSES``
      already excludes it, so the gate itself needs no change here — this only
      automates *reaching* it.
    * ``past_due`` whose date has since moved into the future -> ``active``.
      Something else — a staff member editing the tenant's renewal date, or a
      future billing integration — pushes the date forward; the next sweep
      notices and lets the account back in, the same as a staff "Activate"
      click.

    ``suspended`` and ``cancelled`` are never touched, in either direction.
    Those are deliberate acts (see ``app.api.v1.admin.deactivate_tenant``) and
    must stay that way until a person reverses them — an automatic sweep that
    quietly reactivated a suspended account would turn the deactivate button
    into a temporary measure instead of the deliberate one it is documented
    to be. The query below enforces this by construction: those two statuses
    are simply never selected, not merely skipped by later logic.

    A null ``subscription_renews_at`` is excluded the same way. That is the
    state of every tenant that existed before this column did, and of any
    account staff choose to manage by hand rather than by date — the sweep
    must never invent a lapse for an account nobody told it to watch.

    A third thing happens on the same pass, independent of which direction (or
    neither) a tenant moves in above: a plan switch queued in
    ``pending_plan_id`` (see ``app.api.v1.sync.update_tenant``) is promoted to
    ``plan_id`` once ``renews_at`` is reached — "after the duration of the
    current plan" is exactly this date. Promoted here rather than only on the
    ``past_due`` -> ``active`` branch, so a tenant that is already
    ``past_due`` and stays that way still gets its queued plan, instead of
    the switch being lost because it lapsed before renewing. The one case
    this cannot catch on time is a date extended *before* it is ever reached
    — there is nothing here to notice an early extension, so that switch
    simply waits for whatever date is current when it is finally reached.
    """
    now = now or datetime.now(timezone.utc)
    lapsed = 0
    renewed = 0
    switched = 0

    candidates = db.scalars(
        select(Tenant).where(
            Tenant.status.in_(
                [
                    TenantStatus.trialing.value,
                    TenantStatus.active.value,
                    TenantStatus.past_due.value,
                ]
            ),
            Tenant.subscription_renews_at.is_not(None),
        )
    ).all()

    for tenant in candidates:
        # DateTime(timezone=True) round-trips aware on PostgreSQL and naive on
        # SQLite — the same discrepancy last_run_started_at normalises above.
        # Comparing at the database level would trust every backend to agree
        # on what a naive value means, which is exactly the bug class that
        # made BioTime's own punch windows drift; comparing here, after
        # ensure_aware, does not.
        renews_at = ensure_aware(tenant.subscription_renews_at)
        if renews_at is None:
            continue
        if tenant.status in (TenantStatus.trialing.value, TenantStatus.active.value):
            if renews_at <= now:
                tenant.status = TenantStatus.past_due.value
                lapsed += 1
        elif renews_at > now:
            tenant.status = TenantStatus.active.value
            renewed += 1

        if tenant.pending_plan_id and renews_at <= now:
            new_plan = db.get(SubscriptionPlan, tenant.pending_plan_id)
            tenant.plan_id = tenant.pending_plan_id
            tenant.pending_plan_id = None
            # Same reason a fresh signup's interval is clamped to its plan's
            # floor (app.api.v1.auth.signup): the queued plan may need a
            # slower interval than whatever was set while still on the old
            # one, and nothing else will catch that the moment it lands.
            if new_plan and new_plan.min_sync_interval_minutes:
                tenant.sync_interval_minutes = max(
                    tenant.sync_interval_minutes, new_plan.min_sync_interval_minutes
                )
            switched += 1

    if lapsed or renewed or switched:
        db.commit()
    return {"lapsed": lapsed, "renewed": renewed, "switched": switched}


def renewal_warning(tenant: Tenant, *, now: datetime | None = None) -> dict | None:
    """Close enough to the renewal date to say something about it, or None.

    Only while the account is still current: a lapsed subscription already
    carries its own, louder message (``Tenant.syncable`` is false, and the
    dashboard leads with that) and showing both would bury the one that
    actually matters. The window is ``settings.subscription_warning_days`` —
    the same number both the tenant dashboard and the staff console read, so
    the two surfaces cannot disagree about what counts as "soon".

    ``urgent`` is the same warning, not a second one: within
    ``settings.subscription_urgent_days`` it is still one message, just one
    the UI renders louder — a trial or a paid plan reads the same either way,
    since both are just this account's current ``subscription_renews_at``.
    """
    if tenant.status not in (TenantStatus.trialing.value, TenantStatus.active.value):
        return None
    renews_at = ensure_aware(tenant.subscription_renews_at)
    if renews_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    days_left = (renews_at - now).total_seconds() / 86400
    if days_left > settings.subscription_warning_days:
        return None
    days_left = max(0, round(days_left))
    return {
        "renews_at": renews_at,
        "days_left": days_left,
        "urgent": days_left <= settings.subscription_urgent_days,
    }
