"""Background tasks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import session_scope
from app.models import PunchRecord, PunchState, SyncRun, Tenant, TenantStatus
from app.services.sync_engine import SyncEngine, close_stale_attendances as _close_stale
from app.services.timeutils import ensure_aware, utcnow_naive
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

SYNCABLE = {TenantStatus.trialing.value, TenantStatus.active.value}
#: A tenant whose connections keep failing polls this many times less often, so
#: a dead customer server is not hammered every interval.
SLOW_LANE_MULTIPLIER = 4

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
    """Beat entry point: enqueue a sync for every tenant whose interval elapsed."""
    dispatched = skipped = 0

    with session_scope() as db:
        tenants = db.scalars(
            select(Tenant).where(
                Tenant.sync_enabled.is_(True), Tenant.status.in_(SYNCABLE)
            )
        ).all()

        for tenant in tenants:
            interval = tenant.sync_interval_minutes
            if tenant.consecutive_failures >= settings.max_consecutive_failures:
                interval *= SLOW_LANE_MULTIPLIER

            last = db.scalars(
                select(SyncRun)
                .where(SyncRun.tenant_id == tenant.id)
                .order_by(SyncRun.started_at.desc())
                .limit(1)
            ).first()

            due = last is None or ensure_aware(last.started_at) <= datetime.now(
                timezone.utc
            ) - timedelta(minutes=interval)
            if not due:
                skipped += 1
                continue

            sync_tenant.delay(tenant.id, "schedule")
            dispatched += 1

    return {"dispatched": dispatched, "skipped": skipped}


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
