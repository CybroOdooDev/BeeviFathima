"""What happens to a run when a customer's device platform is unreachable.

The engine already had a per-source handler whose whole purpose was to stop one
dead site from taking the others down with it. It never fired for the commonest
outage there is, because the provider leaked ``httpx.ConnectError`` and that is
not a ``ProviderError`` — so the handler's type filter missed it and the run
died in the catch-all.

These tests pin the behaviour at the engine level, in terms the engine controls
(exception classes), so they hold whatever a provider raises internally.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.integrations.base import ProviderError
from app.models import ConnectionStatus, DeviceSource, PunchRecord
from app.services import sync_engine as engine_mod
from app.services.sync_engine import AllSourcesUnreachable, SyncAborted
from app.core.crypto import encrypt
from tests.conftest import TZ, FakeOdoo, FakeProvider
from tests.test_sync_engine import punch


class DeadProvider:
    """A platform that is switched off."""

    label = "Dead"
    cached_token = None

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def fetch_punches(self, since=None, until=None):
        raise ProviderError(
            "Cannot reach BioTime at http://10.0.0.9:8090: the connection was "
            "refused — nothing is listening on that port."
        )
        yield  # pragma: no cover — makes this a generator, as the real one is

    def close(self):
        pass


def second_source(db, tenant, name="Second Site"):
    row = DeviceSource(
        tenant_id=tenant.id,
        name=name,
        base_url="https://bio2.test",
        username="a",
        password_enc=encrypt("p", tenant.crypto_key),
        server_timezone=TZ,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# Isolation: one dead site must not cost the others their punches
# --------------------------------------------------------------------------- #
def test_one_dead_site_does_not_block_the_other(db, tenant, local_day, monkeypatch):
    """The reason the per-source handler exists, asserted end to end."""
    dead = second_source(db, tenant)
    live_rows = [punch(1, "1001", local_day.replace(hour=8))]

    def build(_tenant, source):
        return DeadProvider() if source.id == dead.id else FakeProvider(live_rows)

    odoo = FakeOdoo()
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", build)
    run = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert run.status == "partial", (
        f"a reachable site synced and another failed: {run.error_message}"
    )
    stored = db.scalars(select_punches()).all()
    assert len(stored) == 1, "the working site's punch must survive its neighbour"
    assert run.error_count == 1

    db.refresh(dead)
    assert dead.status == ConnectionStatus.failed.value
    assert "refused" in (dead.status_message or "")


def test_the_healthy_source_is_still_marked_connected(db, tenant, local_day, monkeypatch):
    """A failure next door must not smear onto a site that answered fine."""
    dead = second_source(db, tenant)
    healthy = db.scalars(
        select_sources().where(DeviceSource.id != dead.id)
    ).first()

    def build(_tenant, source):
        return DeadProvider() if source.id == dead.id else FakeProvider([])

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", build)
    engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    db.refresh(healthy)
    assert healthy.status == ConnectionStatus.connected.value


# --------------------------------------------------------------------------- #
# The message, and the failure streak behind it
# --------------------------------------------------------------------------- #
def test_total_outage_reports_the_cause_not_a_stack_trace(db, tenant, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: DeadProvider())
    run = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert run.status == "failed"
    message = run.error_message or ""
    assert "Unexpected error" not in message, "this outage is entirely expected"
    assert "refused" in message, "the run list must say why without opening the log"
    assert "Traceback" not in message


def test_single_site_message_is_not_padded_with_a_count(db, tenant, monkeypatch):
    """"None of the 1 connected platform(s)" is noise around the real answer."""
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: DeadProvider())
    run = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    message = run.error_message or ""
    assert message.startswith("Cannot reach BioTime")
    assert "None of the 1" not in message
    assert message.count("Cannot reach BioTime") == 1, "said once, not twice"


def test_multi_site_outage_says_how_many_and_why(db, tenant, monkeypatch):
    second_source(db, tenant)
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: DeadProvider())
    run = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    message = run.error_message or ""
    assert "None of the 2" in message, "with several sites the count matters"
    assert "refused" in message, "and so does at least one reason"


def test_total_outage_counts_towards_the_failure_streak(db, tenant, monkeypatch):
    """An outage is exactly what the streak is for.

    ``AllSourcesUnreachable`` subclasses ``SyncAborted``, which is deliberately
    exempt from the streak — so this is the test that keeps the subclass from
    silently inheriting the exemption and leaving a dead customer on the fast
    lane forever.
    """
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: DeadProvider())

    before = tenant.consecutive_failures
    engine_mod.SyncEngine(db, tenant, "test").run_cycle()
    assert tenant.consecutive_failures == before + 1


def test_repeated_outage_eventually_marks_degraded(db, tenant, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: DeadProvider())

    for _ in range(settings.max_consecutive_failures):
        engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    source = db.scalars(select_sources()).first()
    db.refresh(source)
    assert source.status == ConnectionStatus.degraded.value


def test_no_sources_configured_is_still_exempt(db, tenant, monkeypatch):
    """Onboarding is not an outage — the exemption must survive the new subclass."""
    for source in db.scalars(select_sources()).all():
        source.is_active = False
    db.flush()

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    before = tenant.consecutive_failures
    run = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert run.status == "failed"
    assert tenant.consecutive_failures == before, (
        "a half-finished setup must not badge the customer degraded"
    )


def test_unreachable_is_catchable_as_sync_aborted() -> None:
    """Callers that only care 'the run stopped early' keep working."""
    assert issubclass(AllSourcesUnreachable, SyncAborted)


# --------------------------------------------------------------------------- #
# The specific regression, at the seam
# --------------------------------------------------------------------------- #
def test_raw_httpx_error_would_still_be_reported_as_unexpected(db, tenant, monkeypatch):
    """Documents why the fix had to go in the provider, not here.

    Broadening the engine's handler to bare ``Exception`` would hide real bugs,
    so the engine still treats a leaked httpx error as unexpected — which is
    correct, and is why the provider is the thing that must not leak.
    """

    class LeakyProvider(DeadProvider):
        def fetch_punches(self, since=None, until=None):
            raise httpx.ConnectError("[Errno 111] Connection refused")
            yield  # pragma: no cover

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: LeakyProvider())
    run = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert run.status == "failed"
    assert "Unexpected error" in (run.error_message or "")


# -- small helpers so the assertions above stay readable --------------------- #
def select_punches():
    from sqlalchemy import select

    return select(PunchRecord)


def select_sources():
    from sqlalchemy import select

    return select(DeviceSource).where(DeviceSource.is_active.is_(True))


@pytest.fixture(autouse=True)
def _quiet_logs(caplog):
    """These runs log at ERROR by design; keep the output readable."""
    caplog.set_level("CRITICAL", logger="app.services.sync_engine")
