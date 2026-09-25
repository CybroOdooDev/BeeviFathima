"""One account with a BioTime server *and* a standalone ZKTeco terminal.

Both are DeviceSource rows on the same tenant; one sync cycle reads every
active source, and pairing works on each employee's punches from all of them
together. These pin down the three things a customer mixing the two cares
about.
"""
from __future__ import annotations

from sqlalchemy import select

import app.services.sync_engine as engine_mod
from app.core.crypto import encrypt
from app.models import DeviceSource, PunchRecord
from tests.conftest import TZ, FakeOdoo, FakeProvider
from tests.test_sync_engine import punch


def _add_standalone(db, tenant) -> DeviceSource:
    source = DeviceSource(
        tenant_id=tenant.id, name="Warehouse ZK", provider="zkteco_device",
        connection_kind="device", base_url="tcp://10.0.11.43:4370", username="-",
        password_enc=encrypt("0", tenant.crypto_key), server_timezone=TZ, is_active=True,
    )
    db.add(source)
    db.commit()
    return source


def _run(db, tenant, odoo, rows_by_source, monkeypatch):
    def provider_for(t, source):
        rows = rows_by_source.get(source.name)
        if isinstance(rows, Exception):
            class Down(FakeProvider):
                def fetch_punches(self, since=None, until=None):
                    raise rows
            return Down([])
        return FakeProvider(rows or [])

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", provider_for)
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def _on(terminal, row):
    row["terminal_sn"] = terminal
    return row


def test_check_in_on_biotime_and_check_out_on_the_standalone_make_one_shift(
    db, tenant, local_day, monkeypatch
):
    _add_standalone(db, tenant)
    odoo = FakeOdoo()
    result = _run(db, tenant, odoo, {
        "BioTime": [punch(1, "1001", local_day.replace(hour=8))],
        "Warehouse ZK": [_on("ZK-WH-1", punch(1, "1001", local_day.replace(hour=17)))],
    }, monkeypatch)

    assert result.status == "success", result.error_message
    (record,) = odoo.attendances.values()
    assert record["check_in"].hour + 4 == 8 and record["check_out"].hour + 4 == 17


def test_the_same_punch_arriving_through_both_is_counted_once(db, tenant, local_day, monkeypatch):
    """A terminal BioTime also polls reports each punch twice, once per path."""
    _add_standalone(db, tenant)
    odoo = FakeOdoo()
    day_in, day_out = local_day.replace(hour=8), local_day.replace(hour=17)
    _run(db, tenant, odoo, {
        "BioTime": [punch(1, "1001", day_in), punch(2, "1001", day_out)],
        "Warehouse ZK": [punch(90, "1001", day_in), punch(91, "1001", day_out)],
    }, monkeypatch)

    assert len(odoo.attendances) == 1
    states = sorted(p.process_state for p in db.scalars(select(PunchRecord)))
    assert states == ["skipped", "skipped", "synced", "synced"]


def test_a_standalone_terminal_being_offline_does_not_hold_up_biotime(
    db, tenant, local_day, monkeypatch
):
    from app.integrations.base import ProviderError

    standalone = _add_standalone(db, tenant)
    odoo = FakeOdoo()
    result = _run(db, tenant, odoo, {
        "BioTime": [punch(1, "1001", local_day.replace(hour=8)),
                    punch(2, "1001", local_day.replace(hour=17))],
        "Warehouse ZK": ProviderError("no route to 10.0.11.43"),
    }, monkeypatch)

    assert result.status == "partial"
    assert len(odoo.attendances) == 1
    db.refresh(standalone)
    assert standalone.status == "failed" and standalone.cursor_punch_time is None