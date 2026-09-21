"""Background tasks."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import session_scope
from app.models import PunchRecord, PunchState, Tenant
from app.services.scheduling import (
    SLOW_LANE_MULTIPLIER,  # noqa: F401 — re-exported; imported from here historically
    SYNCABLE,
    claim_lease,
    due_tenants,
    owner_id,
    sweep_subscriptions as _sweep_subscriptions,
)
from app.services.sync_engine import SyncEngine, close_stale_attendances as _close_stale
from app.services.timeutils import utcnow_naive
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

_redis_client = None


def get_redis():
    """Build the Redis client lazily.

    Connecting at import time means an unset or malformed REDIS_URL takes down
    every module that merely imports this one — including the API's sync
    endpoints, which only need the queue at call time.
    """
    global _redis_client
    if _redis_client is None:
        if not settings.redis_url:
            return None
        try:
            import redis

            _redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis unusable (%s); per-tenant sync locking is off", exc)
            return None
    return _redis_client


@celery_app.task(name="app.workers.tasks.dispatch_due_tenants")
def dispatch_due_tenants() -> dict[str, int]:
    """Beat entry point: enqueue a sync for every tenant whose interval elapsed.

    The due-ness rule lives in ``services.scheduling`` and is shared with the
    in-process scheduler. It used to be written out here as well, which is how a
    customer's 15 minutes ends up meaning two different things depending on
    whether they deployed a worker.

    Claiming the same lease does two jobs: it stamps the heartbeat the dashboard
    reads, so the UI reports a running schedule under Celery too, and it keeps a
    second beat — the classic outcome of scaling the beat container — from
    double-dispatching every tenant.
    """
    with session_scope() as db:
        if not claim_lease(
            db,
            owner=owner_id(),
            mode="celery",
            ttl_seconds=settings.scheduler_lease_ttl_seconds,
        ):
            log.info("Another scheduler holds the lease; not dispatching")
            return {"dispatched": 0, "skipped": 0, "status": "lease_held_elsewhere"}

        due = due_tenants(db)
        for tenant in due:
            sync_tenant.delay(tenant.id, "schedule")

        return {"dispatched": len(due), "skipped": 0}


@celery_app.task(
    name="app.workers.tasks.sync_tenant", bind=True, max_retries=2, default_retry_delay=120
)
def sync_tenant(self, tenant_id: str, triggered_by: str = "schedule") -> dict[str, object]:
    """Run one cycle. A Redis lock guarantees no tenant syncs twice at once."""
    client = get_redis()
    lock = (
        client.lock(f"biobridge:sync:lock:{tenant_id}", timeout=15 * 60, blocking=False)
        if client
        else None
    )
    if lock is not None and not lock.acquire(blocking=False):
        log.info("Sync already running for tenant %s — skipping", tenant_id)
        return {"status": "locked", "tenant_id": tenant_id}

    try:
        with session_scope() as db:
            tenant = db.get(Tenant, tenant_id)
            if tenant is None:
                return {"status": "not_found", "tenant_id": tenant_id}
            if tenant.status not in SYNCABLE:
                return {"status": "inactive", "tenant_id": tenant_id}

            run = SyncEngine(db, tenant, triggered_by).run_cycle()
            return {
                "status": run.status,
                "tenant_id": tenant_id,
                "punches_new": run.punches_new,
                "attendances_created": run.attendances_created,
                "attendances_closed": run.attendances_closed,
                "errors": run.error_count,
            }
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:  # noqa: BLE001 — already expired
                pass


@celery_app.task(name="app.workers.tasks.close_stale_attendances")
def close_stale_attendances() -> dict[str, int]:
    closed = 0
    with session_scope() as db:
        for tenant in db.scalars(select(Tenant).where(Tenant.status.in_(SYNCABLE))).all():
            try:
                closed += _close_stale(db, tenant)
            except Exception as exc:  # noqa: BLE001 — one tenant must not stop the rest
                log.warning("Stale-close failed for %s: %s", tenant.slug, exc)
    return {"closed": closed}


@celery_app.task(name="app.workers.tasks.sweep_subscriptions")
def sweep_subscriptions() -> dict[str, int]:
    """Beat entry point for the renewal-date check.

    Runs on its own hourly slot rather than the tenant-sync one — renewal
    dates do not move minute to minute — and shares its logic with the
    in-process scheduler's equivalent timer, so a customer's account lapses
    (or comes back) on the same rule whichever scheduler is running. See
    app.services.scheduling.sweep_subscriptions for what actually moves and
    what is deliberately left alone.
    """
    with session_scope() as db:
        return _sweep_subscriptions(db)


@celery_app.task(name="app.workers.tasks.prune_old_punches")
def prune_old_punches(retain_days: int = 180) -> dict[str, int]:
    """Keep the ledger bounded. Only fully-resolved punches are eligible."""
    cutoff = utcnow_naive() - timedelta(days=retain_days)
    with session_scope() as db:
        result = db.execute(
            delete(PunchRecord).where(
                PunchRecord.punch_time_utc < cutoff,
                PunchRecord.process_state.in_(
                    [PunchState.synced.value, PunchState.skipped.value]
                ),
            )
        )
        return {"deleted": result.rowcount or 0}
