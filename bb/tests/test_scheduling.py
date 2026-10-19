"""The clock: who is due, and who is allowed to dispatch.

These are the tests that stand between "we have a scheduler" and "attendance
arrives without anyone pressing anything". The failures they catch are all
quiet ones — a schedule that never fires, one that fires twice, one that drifts
later every run, one that keeps hammering a customer's dead server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.models import Base, DeviceSource, SyncRun, Tenant
from app.services.scheduling import (
    SLOW_LANE_MULTIPLIER,
    claim_lease,
    due_tenants,
    effective_interval,
    is_due,
    next_run_at,
    release_lease,
    scheduler_health,
)


def _run(db, tenant, *, minutes_ago: int, status: str = "success") -> SyncRun:
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    row = SyncRun(
        tenant_id=tenant.id,
        triggered_by="schedule",
        status=status,
        started_at=started,
        finished_at=started + timedelta(seconds=20),
    )
    db.add(row)
    db.commit()
    return row


# ===========================================================================
# Due-ness
# ===========================================================================
def test_a_tenant_that_has_never_synced_is_due_immediately(db, tenant):
    """Otherwise a new customer connects both sides and watches nothing happen
    for fifteen minutes, which reads as broken."""
    assert is_due(db, tenant)
    assert tenant in due_tenants(db)


def test_a_tenant_with_nothing_connected_is_not_dispatched(db, tenant):
    """It is unconfigured, not failing.

    Dispatching writes a failed run saying "nothing is connected" — seconds
    after signup, so the first thing a new customer sees on their dashboard is
    *Last sync: failed*, before they have done anything.
    """
    for source in db.scalars(select(DeviceSource)).all():
        source.is_active = False
    db.commit()

    assert due_tenants(db) == []
    # Still *due* by the clock — the gate is about readiness, so the UI can go
    # on showing when it would run.
    assert is_due(db, tenant)


def test_reconnecting_a_source_makes_it_dispatchable_again(db, tenant):
    for source in db.scalars(select(DeviceSource)).all():
        source.is_active = False
    db.commit()
    assert due_tenants(db) == []

    for source in db.scalars(select(DeviceSource)).all():
        source.is_active = True
    db.commit()
    assert tenant in due_tenants(db)


def test_a_tenant_inside_its_interval_is_not_due(db, tenant):
    tenant.sync_interval_minutes = 15
    _run(db, tenant, minutes_ago=5)
    assert not is_due(db, tenant)
    assert due_tenants(db) == []


def test_a_tenant_past_its_interval_is_due(db, tenant):
    tenant.sync_interval_minutes = 15
    _run(db, tenant, minutes_ago=16)
    assert is_due(db, tenant)


def test_due_is_measured_from_the_start_of_the_last_run(db, tenant):
    """A slow cycle must not push the schedule later with every run.

    Measured from the finish, a sync that takes four minutes on a fifteen-minute
    interval drifts by four minutes each time; by the end of the day the
    customer's "every 15 minutes" has become something else entirely.
    """
    tenant.sync_interval_minutes = 15
    started = datetime.now(timezone.utc) - timedelta(minutes=16)
    db.add(
        SyncRun(
            tenant_id=tenant.id,
            triggered_by="schedule",
            status="success",
            started_at=started,
            # Finished only a minute ago: a ten-minute cycle.
            finished_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    db.commit()
    assert is_due(db, tenant)


def test_sync_disabled_excludes_the_tenant_entirely(db, tenant):
    tenant.sync_enabled = False
    db.commit()
    assert next_run_at(db, tenant) is None
    assert not is_due(db, tenant)
    assert due_tenants(db) == []


@pytest.mark.parametrize("status", ["suspended", "cancelled", "past_due"])
def test_a_non_syncable_status_is_never_due(db, tenant, status):
    """A suspended tenant keeps its data and its screens, and stops costing
    anybody polling traffic."""
    tenant.status = status
    db.commit()
    assert next_run_at(db, tenant) is None
    assert due_tenants(db) == []


def test_repeated_failures_widen_the_interval(db, tenant):
    """A customer's BioTime box that has been off for a week should not be hit
    every 15 minutes forever."""
    tenant.sync_interval_minutes = 15
    tenant.consecutive_failures = settings.max_consecutive_failures
    db.commit()

    assert effective_interval(tenant) == 15 * SLOW_LANE_MULTIPLIER

    _run(db, tenant, minutes_ago=20, status="error")
    assert not is_due(db, tenant), "the slow lane should hold it back"

    # One success clears the counter, and the normal interval applies again.
    tenant.consecutive_failures = 0
    db.commit()
    assert is_due(db, tenant)


def test_the_interval_is_never_below_one_minute(db, tenant):
    """A zero would make next_run_at equal the last run and spin the loop."""
    tenant.sync_interval_minutes = 0
    assert effective_interval(tenant) == 1


def test_next_run_at_is_timezone_aware(db, tenant):
    """SyncRun.started_at comes back naive on SQLite and aware on PostgreSQL.

    A naive value leaking out of here raises "can't compare offset-naive and
    offset-aware" in the loop — on Postgres only, which is to say in production
    only.
    """
    _run(db, tenant, minutes_ago=5)
    due_at = next_run_at(db, tenant)
    assert due_at is not None and due_at.tzinfo is not None


# ===========================================================================
# The lease
# ===========================================================================
def test_one_of_two_processes_wins_the_lease(db):
    assert claim_lease(db, owner="host-a:1", mode="inprocess", ttl_seconds=180)
    assert not claim_lease(db, owner="host-b:2", mode="inprocess", ttl_seconds=180)


def test_the_holder_can_renew_its_own_lease(db):
    assert claim_lease(db, owner="host-a:1", mode="inprocess", ttl_seconds=180)
    assert claim_lease(db, owner="host-a:1", mode="inprocess", ttl_seconds=180)


def test_an_expired_lease_is_taken_over(db):
    """This is the entire failover story: a SIGKILLed process releases nothing,
    and the next tick past expiry takes over."""
    assert claim_lease(db, owner="dead-host:1", mode="inprocess", ttl_seconds=-1)
    assert claim_lease(db, owner="live-host:2", mode="inprocess", ttl_seconds=180)


def test_releasing_hands_over_immediately(db):
    """A rolling deploy should not wait out the TTL before anything is
    dispatched."""
    assert claim_lease(db, owner="old:1", mode="inprocess", ttl_seconds=180)
    release_lease(db, owner="old:1")
    assert claim_lease(db, owner="new:2", mode="inprocess", ttl_seconds=180)


def test_releasing_someone_elses_lease_does_nothing(db):
    assert claim_lease(db, owner="holder:1", mode="inprocess", ttl_seconds=180)
    release_lease(db, owner="impostor:2")
    assert not claim_lease(db, owner="impostor:2", mode="inprocess", ttl_seconds=180)


def test_the_lease_survives_a_fresh_database(db):
    """The singleton row is created on first claim, so nothing has to seed it."""
    assert claim_lease(db, owner="first:1", mode="celery", ttl_seconds=180)
    assert scheduler_health(db)["mode"] == "celery"


# ===========================================================================
# Health, which is what the UI and the monitor read
# ===========================================================================
def test_health_reports_not_running_before_anything_ticks(db):
    state = scheduler_health(db)
    assert state["running"] is False
    assert state["last_tick_at"] is None


def test_health_reports_running_right_after_a_tick(db):
    claim_lease(db, owner="host-a:1", mode="inprocess", ttl_seconds=180)
    state = scheduler_health(db)
    assert state["running"] is True
    assert state["owner"] == "host-a:1"
    assert state["seconds_since_tick"] < 5


def test_health_goes_stale_when_ticks_stop(db):
    """The failure this exists for: a process that is gone still holds a row
    saying it once ran. Age, not presence, decides."""
    claim_lease(db, owner="host-a:1", mode="inprocess", ttl_seconds=180)
    assert scheduler_health(db, stale_after_seconds=-1)["running"] is False


# ===========================================================================
# Multi-tenant dispatch
# ===========================================================================
def test_due_tenants_selects_only_the_ones_that_are_due(db, tenant):
    other = Tenant(name="Second", slug="second", status="active", timezone="UTC")
    third = Tenant(name="Third", slug="third", status="active", timezone="UTC")
    db.add_all([other, third])
    db.commit()

    tenant.sync_interval_minutes = 15
    other.sync_interval_minutes = 15
    third.sync_interval_minutes = 60
    db.commit()

    _run(db, tenant, minutes_ago=20)  # due
    _run(db, other, minutes_ago=2)  # not due
    _run(db, third, minutes_ago=30)  # not due, longer interval

    slugs = {t.slug for t in due_tenants(db)}
    assert slugs == {"gulf-steel"}


def test_two_sessions_on_one_database_still_yield_one_dispatcher():
    """The case the lease exists for: `uvicorn --workers 4`, or three pods.

    Separate sessions on a shared database, as in production — not two handles
    on one session, which would prove nothing about concurrency.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    a, b, c = Session(), Session(), Session()
    try:
        results = [
            claim_lease(db, owner=f"worker:{i}", mode="inprocess", ttl_seconds=180)
            for i, db in enumerate((a, b, c))
        ]
        assert sum(results) == 1, "exactly one process may dispatch"
    finally:
        for db in (a, b, c):
            db.close()
