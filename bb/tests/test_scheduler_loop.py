"""The dispatch loop's own behaviour, with the database stubbed out.

``test_scheduling.py`` covers the rules; this covers the loop that acts on them.
The interesting cases are all about restraint: not dispatching when another
process holds the lease, and not starting a cycle on top of one that is still
running.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.scheduler import Scheduler


@pytest.mark.asyncio
async def test_a_tick_dispatches_every_due_tenant(monkeypatch):
    scheduler = Scheduler()
    ran: list[str] = []

    monkeypatch.setattr(scheduler, "_claim_and_select", lambda: (True, ["t1", "t2"]))
    monkeypatch.setattr(Scheduler, "_sync_blocking", staticmethod(ran.append))

    await scheduler._tick()
    await asyncio.sleep(0.2)

    assert sorted(ran) == ["t1", "t2"]


@pytest.mark.asyncio
async def test_a_tick_dispatches_nothing_without_the_lease(monkeypatch):
    """With three replicas, two of them must do nothing every minute."""
    scheduler = Scheduler()
    ran: list[str] = []

    monkeypatch.setattr(scheduler, "_claim_and_select", lambda: (False, []))
    monkeypatch.setattr(Scheduler, "_sync_blocking", staticmethod(ran.append))

    await scheduler._tick()
    await asyncio.sleep(0.1)

    assert ran == []


@pytest.mark.asyncio
async def test_a_tenant_still_syncing_is_skipped(monkeypatch):
    """A cycle slower than the tick must not be started on top of itself.

    Two concurrent cycles for one tenant race on the same open-shift state and
    can write a duplicate attendance — the ledger's unique key stops the punch
    being stored twice, but not two Odoo writes from two engines that each
    believe they hold the open record.
    """
    scheduler = Scheduler()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    def slow(tenant_id: str) -> None:
        calls.append(tenant_id)
        started.set()
        # Block the worker thread until the test says otherwise.
        asyncio.run_coroutine_threadsafe(
            _wait(release), scheduler._loop_for_test
        ).result()

    async def _wait(event: asyncio.Event) -> None:
        await event.wait()

    scheduler._loop_for_test = asyncio.get_running_loop()
    monkeypatch.setattr(scheduler, "_claim_and_select", lambda: (True, ["t1"]))
    monkeypatch.setattr(Scheduler, "_sync_blocking", staticmethod(slow))

    await scheduler._tick()
    await asyncio.wait_for(started.wait(), timeout=2)

    # Second tick while the first cycle is still in flight.
    await scheduler._tick()
    await asyncio.sleep(0.1)

    assert calls == ["t1"], "the in-flight tenant should have been skipped"

    release.set()
    await asyncio.sleep(0.2)
    assert not scheduler._in_flight


@pytest.mark.asyncio
async def test_a_failing_sync_does_not_stop_the_others(monkeypatch):
    """One customer's unreachable Odoo must not hold up everybody else's."""
    scheduler = Scheduler()
    ran: list[str] = []

    def flaky(tenant_id: str) -> None:
        ran.append(tenant_id)
        if tenant_id == "bad":
            raise RuntimeError("Odoo unreachable")

    monkeypatch.setattr(
        scheduler, "_claim_and_select", lambda: (True, ["bad", "good"])
    )
    monkeypatch.setattr(Scheduler, "_sync_blocking", staticmethod(flaky))

    await scheduler._tick()
    await asyncio.sleep(0.3)

    assert sorted(ran) == ["bad", "good"]
    assert not scheduler._in_flight, "a failed cycle must still clear its slot"


@pytest.mark.asyncio
async def test_concurrency_is_bounded(monkeypatch):
    """Fifty tenants must not mean fifty simultaneous connections to fifty
    customer LANs."""
    scheduler = Scheduler()
    scheduler._semaphore = asyncio.Semaphore(2)
    live = 0
    peak = 0
    lock = asyncio.Lock()

    async def occupy(_tenant_id: str) -> None:
        nonlocal live, peak
        async with lock:
            live += 1
            peak = max(peak, live)
        await asyncio.sleep(0.05)
        async with lock:
            live -= 1

    select = lambda: (True, [f"t{i}" for i in range(8)])  # noqa: E731
    monkeypatch.setattr(scheduler, "_claim_and_select", select)

    real_to_thread = asyncio.to_thread

    def fake_to_thread(fn, *args, **kwargs):
        """Replace only the sync hop, so the bound under test is the semaphore
        and not whatever the thread pool happens to allow. The lease read still
        goes through the real thing."""
        if fn is select:
            return real_to_thread(fn, *args, **kwargs)
        return occupy(*args)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    await scheduler._tick()
    await asyncio.sleep(0.6)

    assert peak <= 2, f"ran {peak} at once with a limit of 2"


@pytest.mark.asyncio
async def test_the_loop_survives_a_tick_that_raises(monkeypatch):
    """The loop is the one thing that must not die.

    A tick that throws has already failed; a loop that exits means the
    customer's attendance stops arriving and nothing says so.
    """
    scheduler = Scheduler()
    ticks = 0

    async def exploding_tick():
        nonlocal ticks
        ticks += 1
        raise RuntimeError("database went away")

    monkeypatch.setattr(scheduler, "_tick", exploding_tick)
    monkeypatch.setattr(
        "app.services.scheduler.settings.scheduler_tick_seconds", 0.05
    )

    task = asyncio.create_task(scheduler._loop())
    await asyncio.sleep(1.8)
    assert not task.done(), "the loop exited on a failing tick"
    assert ticks >= 2, f"the loop stopped ticking after {ticks} tick(s)"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
