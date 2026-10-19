"""The plan's device allowance: over-limit terminals are recorded, their
punches kept but held from Odoo, and released once the plan covers them."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Device, PunchRecord, PunchState, SubscriptionPlan
from app.services.device_limits import over_limit_device_ids
from tests.test_sync_engine import punch, run
from tests.conftest import FakeOdoo


def _on_plan(db, tenant, max_devices):
    plan = SubscriptionPlan(name=f"Cap {max_devices}", max_devices=max_devices)
    db.add(plan)
    db.flush()
    tenant.plan_id = plan.id
    db.commit()
    return plan


def _shift(pid, emp, day, sn):
    a = punch(pid, emp, day.replace(hour=8))
    b = punch(pid + 1, emp, day.replace(hour=17))
    a["terminal_sn"] = b["terminal_sn"] = sn
    return [a, b]


def test_an_unknown_terminal_is_recorded_from_its_first_punch(db, tenant, local_day, monkeypatch):
    run(db, tenant, FakeOdoo(), _shift(1, "1001", local_day, "GATE-NEW"), monkeypatch)
    serials = {d.serial_number for d in db.scalars(select(Device)).all()}
    assert "GATE-NEW" in serials


def test_punches_from_a_terminal_over_the_limit_are_held_not_lost(db, tenant, local_day, monkeypatch):
    _on_plan(db, tenant, max_devices=1)  # the fixture's GATE-01 fills it
    odoo = FakeOdoo()
    rows = _shift(1, "1001", local_day, "GATE-01") + _shift(3, "1002", local_day, "GATE-02")
    result = run(db, tenant, odoo, rows, monkeypatch)
    assert result.status == "success", result.error_message

    states = {p.external_id: p.process_state for p in db.scalars(select(PunchRecord)).all()}
    assert states["1"] == states["2"] == PunchState.synced.value
    assert states["3"] == states["4"] == PunchState.held.value
    assert len(odoo.attendances) == 1, "only the covered terminal's shift reached Odoo"
    held = db.scalar(select(PunchRecord).where(PunchRecord.external_id == "3"))
    assert "1-device limit" in held.error_message


def test_upgrading_releases_held_punches_on_the_next_run(db, tenant, local_day, monkeypatch):
    plan = _on_plan(db, tenant, max_devices=1)
    odoo = FakeOdoo()
    rows = _shift(1, "1001", local_day, "GATE-01") + _shift(3, "1002", local_day, "GATE-02")
    run(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 1

    plan.max_devices = 2
    db.commit()
    run(db, tenant, odoo, [], monkeypatch)
    assert len(odoo.attendances) == 2
    assert all(p.process_state == PunchState.synced.value for p in db.scalars(select(PunchRecord)).all())


def test_the_oldest_terminals_fill_the_allowance(db, tenant, local_day, monkeypatch):
    _on_plan(db, tenant, max_devices=1)
    run(db, tenant, FakeOdoo(), _shift(1, "1001", local_day, "GATE-02"), monkeypatch)
    by_serial = {d.serial_number: d.id for d in db.scalars(select(Device)).all()}
    assert over_limit_device_ids(db, tenant) == {by_serial["GATE-02"]}, \
        "a newly seen terminal never pushes out one that was already working"


def test_no_limit_means_nothing_is_held(db, tenant, local_day, monkeypatch):
    rows = _shift(1, "1001", local_day, "GATE-01") + _shift(3, "1002", local_day, "GATE-02")
    run(db, tenant, FakeOdoo(), rows, monkeypatch)
    assert not any(p.process_state == PunchState.held.value for p in db.scalars(select(PunchRecord)).all())


def test_sync_brings_back_a_deleted_terminal_that_has_punches(db, tenant, local_day, monkeypatch):
    run(db, tenant, FakeOdoo(), _shift(1, "1001", local_day, "GATE-01"), monkeypatch)
    gate = db.scalar(select(Device).where(Device.serial_number == "GATE-01"))
    db.query(PunchRecord).filter(PunchRecord.device_id == gate.id).update({"device_id": None})
    db.delete(gate)
    db.commit()
    assert db.scalar(select(Device).where(Device.serial_number == "GATE-01")) is None

    run(db, tenant, FakeOdoo(), [], monkeypatch)  # a sync that brings nothing new

    back = db.scalar(select(Device).where(Device.serial_number == "GATE-01"))
    assert back is not None and back.punch_count >= 2
    assert all(p.device_id == back.id for p in db.scalars(select(PunchRecord)).all() if p.terminal_sn == "GATE-01")
