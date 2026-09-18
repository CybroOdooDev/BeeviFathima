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
    DeviceSource,
    SchedulerLease,
    SyncRun,
    Tenant,
    TenantStatus,
)
from app.services.timeutils import ensure_aware, utcnow_naive

log = logging.getLogger(__name__)

#: Statuses that are allowed to sync. A suspended or cancelled tenant keeps its
#: data and its screens, and stops costing anybody polling traffic.
SYNCABLE: frozenset[str] = frozenset(
    {TenantStatus.trialing.value, TenantStatus.active.value}
)

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
