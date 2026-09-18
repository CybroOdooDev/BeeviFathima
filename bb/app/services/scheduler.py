"""The scheduling loop that runs inside the API process.

Why this exists at all: the product's promise is "connect both sides and
attendance keeps arriving". A schedule that only works once the customer has
also deployed Redis, a Celery worker and a beat process is not that promise —
it is four services to keep alive for a job that is one timer and a database
query. So the API can run the clock itself, and Celery becomes what it should
be: the option you reach for when one box is no longer enough.

Three things make it safe to run in every replica:

* **A database lease.** Only the holder dispatches, so ``--workers 4`` or three
  pods behind a load balancer still produce one sync per tenant per interval.
* **An in-flight set.** A cycle slower than the tick is not started again on
  top of itself; the tenant is simply skipped until it finishes.
* **A thread pool.** The engine is blocking (``requests`` and ``xmlrpc``), so
  running it on the event loop would stall every HTTP request served by this
  process for the length of a sync.

Cancellation is cooperative: shutdown stops the loop, waits briefly for
in-flight cycles, and drops the lease so a replacement process starts
dispatching at once instead of waiting out the TTL.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.core.config import settings
from app.db.session import session_scope
from app.models import Tenant
from app.services.scheduling import (
    SYNCABLE,
    claim_lease,
    due_tenants,
    owner_id,
    release_lease,
)
from app.services.sync_engine import SyncEngine, close_stale_attendances

log = logging.getLogger(__name__)

#: Stale-close runs on its own slower cadence. An employee who forgot to badge
#: out holds an open record that blocks every later check-in for that person, so
#: it has to happen without anyone asking — but hourly is plenty.
STALE_CLOSE_EVERY_SECONDS = 3600


class Scheduler:
    """Owns one asyncio task. Start it in ``lifespan``, stop it on shutdown."""

    def __init__(self) -> None:
        self.owner = owner_id()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._in_flight: set[str] = set()
        self._semaphore = asyncio.Semaphore(max(1, settings.scheduler_concurrency))
        self._last_stale_close: datetime | None = None
        self.ticks = 0
        self.dispatched = 0

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="biobridge-scheduler")
        log.info(
            "Scheduler started in-process as %s (tick %ss, concurrency %s)",
            self.owner,
            settings.scheduler_tick_seconds,
            settings.scheduler_concurrency,
        )

    async def stop(self, drain_seconds: float = 20.0) -> None:
        """Stop ticking, let running cycles finish, then drop the lease."""
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

        deadline = asyncio.get_event_loop().time() + drain_seconds
        while self._in_flight and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.25)
        if self._in_flight:
            log.warning(
                "Shutting down with %d sync(s) still running; they will be "
                "redone next tick — the ledger is keyed on the vendor's punch "
                "id, so nothing is written twice",
                len(self._in_flight),
            )

        try:
            await asyncio.to_thread(self._release)
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            log.debug("Could not release the scheduler lease: %s", exc)
        log.info("Scheduler stopped after %d tick(s), %d dispatch(es)",
                 self.ticks, self.dispatched)

    def _release(self) -> None:
        with session_scope() as db:
            release_lease(db, owner=self.owner)

    # --- the loop ----------------------------------------------------------
    async def _loop(self) -> None:
        # A short initial delay lets the process finish booting — and staggers
        # replicas that were all started by the same deploy, so they do not
        # contend for the lease on the same second.
        await asyncio.sleep(1.5)
        while not self._stopping.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # The loop is the one thing that must not die. A tick that
                # raises has already failed; a loop that exits means the
                # customer's attendance silently stops arriving.
                log.exception("Scheduler tick failed; continuing")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=settings.scheduler_tick_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        self.ticks += 1
        held, tenant_ids = await asyncio.to_thread(self._claim_and_select)
        if not held:
            # Another process holds the lease. Normal with more than one replica.
            log.debug("Scheduler lease held elsewhere; skipping this tick")
            return

        for tenant_id in tenant_ids:
            if tenant_id in self._in_flight:
                log.info("Tenant %s is still syncing; skipping this tick", tenant_id)
                continue
            self._in_flight.add(tenant_id)
            self.dispatched += 1
            asyncio.create_task(self._run_one(tenant_id))

        if self._stale_close_due():
            asyncio.create_task(self._run_stale_close())

    def _claim_and_select(self) -> tuple[bool, list[str]]:
        """Claim the lease and read the due list — one short database visit.

        Ids, not ORM objects: each cycle opens its own session, and a detached
        instance from this one would blow up the moment the engine touched a
        lazy attribute.
        """
        with session_scope() as db:
            if not claim_lease(
                db,
                owner=self.owner,
                mode="inprocess",
                ttl_seconds=settings.scheduler_lease_ttl_seconds,
            ):
                return False, []
            return True, [t.id for t in due_tenants(db)]

    # --- running a cycle ---------------------------------------------------
    async def _run_one(self, tenant_id: str) -> None:
        try:
            async with self._semaphore:
                await asyncio.to_thread(self._sync_blocking, tenant_id)
        except Exception:  # noqa: BLE001 — one tenant must not stop the rest
            log.exception("Scheduled sync failed for tenant %s", tenant_id)
        finally:
            self._in_flight.discard(tenant_id)

    @staticmethod
    def _sync_blocking(tenant_id: str) -> None:
        with session_scope() as db:
            tenant = db.get(Tenant, tenant_id)
            if tenant is None or tenant.status not in SYNCABLE:
                return
            run = SyncEngine(db, tenant, "schedule").run_cycle()
            log.info(
                "Scheduled sync %s for %s: %d new punch(es), %d created, %d closed, "
                "%d error(s)",
                run.status,
                tenant.slug,
                run.punches_new,
                run.attendances_created,
                run.attendances_closed,
                run.error_count,
            )

    # --- housekeeping ------------------------------------------------------
    def _stale_close_due(self) -> bool:
        now = datetime.now(timezone.utc)
        if self._last_stale_close is None:
            self._last_stale_close = now  # not on the first tick — let a sync run first
            return False
        if (now - self._last_stale_close).total_seconds() < STALE_CLOSE_EVERY_SECONDS:
            return False
        self._last_stale_close = now
        return True

    async def _run_stale_close(self) -> None:
        try:
            closed = await asyncio.to_thread(self._stale_close_blocking)
            if closed:
                log.info("Closed %d stale open attendance record(s)", closed)
        except Exception:  # noqa: BLE001
            log.exception("Stale-attendance close failed")

    @staticmethod
    def _stale_close_blocking() -> int:
        from sqlalchemy import select

        closed = 0
        with session_scope() as db:
            for tenant in db.scalars(
                select(Tenant).where(Tenant.status.in_(SYNCABLE))
            ).all():
                try:
                    closed += close_stale_attendances(db, tenant)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Stale-close failed for %s: %s", tenant.slug, exc)
        return closed


scheduler = Scheduler()
