"""Every hr.attendance BioBridge creates points at the device it was punched on.

Two gaps closed (app/services/device_links.py):

* going forward, a punch fetched before its terminal was imported into
  BioBridge is still traced to it, by serial number, when its attendance
  is pushed;
* for records already in Odoo without a device, ``link_attendance_devices``
  (and ``tools/link_attendance_devices.py``) fills it in over XML-RPC.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

import tools.link_attendance_devices as tool_mod
from app.integrations.odoo import OdooError
from app.models import Device, DeviceSource, OdooConnection, PunchRecord
from app.services.device_links import link_attendance_devices
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run


def _tracking(db, tenant, on=True):
    conn = db.scalar(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id))
    conn.has_device_tracking = on
    db.commit()


def _shift(emp, day, terminal="GATE-01", first_id=1):
    rows = [punch(first_id, emp, day.replace(hour=8)), punch(first_id + 1, emp, day.replace(hour=17))]
    for r in rows:
        r["terminal_sn"] = terminal
    return rows


# --------------------------------------------------------------------------- #
# Going forward
# --------------------------------------------------------------------------- #
def test_punches_fetched_before_their_terminal_was_imported_still_get_its_device(
    db, tenant, local_day, monkeypatch
):
    _tracking(db, tenant)
    device = db.scalar(select(Device))
    source_id, serial = device.source_id, device.serial_number
    db.delete(device)  # the terminal isn't imported yet when the punches arrive
    db.commit()

    rows = _shift("1001", local_day)
    # Cycle 1: badge unknown to Odoo, so the punches are stored but not pushed.
    run(db, tenant, FakeOdoo(employees={}), rows, monkeypatch)
    assert all(p.device_id is None for p in db.scalars(select(PunchRecord)))

    # "Import terminals", then Odoo learns the badge; cycle 2 pushes.
    db.add(Device(tenant_id=tenant.id, source_id=source_id, serial_number=serial, is_enabled=True))
    db.commit()
    odoo = FakeOdoo()
    run(db, tenant, odoo, rows, monkeypatch)

    (record,) = odoo.attendances.values()
    assert record["device_id"] == odoo.devices["GATE-01"]
    check_in = db.scalars(select(PunchRecord).order_by(PunchRecord.punch_time_utc)).first()
    assert check_in.device_id is not None, "the link found by serial number is kept"


# --------------------------------------------------------------------------- #
# Records already in Odoo
# --------------------------------------------------------------------------- #
def test_links_records_pushed_before_device_tracking_was_on(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    run(db, tenant, odoo, _shift("1001", local_day), monkeypatch)  # tracking off
    (attendance_id,) = odoo.attendances
    assert odoo.attendances[attendance_id]["device_id"] is None

    _tracking(db, tenant)
    report = link_attendance_devices(db, tenant, odoo, apply=False)
    assert (report.attendances, report.to_link, report.missing) == (1, 1, {"GATE-01": [attendance_id]})
    assert odoo.attendances[attendance_id]["device_id"] is None, "report only writes nothing"
    assert not any(c.startswith(("upsert_device", "set_attendance_device")) for c in odoo.calls)

    report = link_attendance_devices(db, tenant, odoo, apply=True)
    assert report.linked == 1
    assert odoo.attendances[attendance_id]["device_id"] == odoo.devices["GATE-01"]

    again = link_attendance_devices(db, tenant, odoo, apply=True)
    assert (again.to_link, again.already_linked, again.linked) == (0, 1, 0)


def test_a_device_already_set_in_odoo_is_never_overwritten(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    run(db, tenant, odoo, _shift("1001", local_day), monkeypatch)
    (attendance_id,) = odoo.attendances
    odoo.attendances[attendance_id]["device_id"] = 999  # someone set it by hand
    _tracking(db, tenant)

    report = link_attendance_devices(db, tenant, odoo, apply=True)
    assert (report.already_linked, report.linked) == (1, 0)
    assert odoo.attendances[attendance_id]["device_id"] == 999


def test_a_record_with_no_traceable_terminal_is_counted_not_guessed(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    run(db, tenant, odoo, _shift("1001", local_day, terminal="NOT-IMPORTED"), monkeypatch)
    _tracking(db, tenant)

    report = link_attendance_devices(db, tenant, odoo, apply=True)
    assert (report.attendances, report.no_terminal, report.linked) == (1, 1, 0)


def test_one_terminal_odoo_rejects_does_not_stop_the_others(db, tenant, local_day, monkeypatch):
    source = db.scalar(select(DeviceSource))
    db.add(Device(tenant_id=tenant.id, source_id=source.id, serial_number="GATE-02", is_enabled=True))
    db.commit()

    class Picky(FakeOdoo):
        def upsert_device(self, serial_number, **kw):
            if serial_number == "GATE-01":
                raise OdooError("no access to devices")
            return super().upsert_device(serial_number, **kw)

    odoo = Picky()
    rows = _shift("1001", local_day, "GATE-01", 1) + _shift("1002", local_day, "GATE-02", 3)
    run(db, tenant, odoo, rows, monkeypatch)  # tracking off: no devices yet
    _tracking(db, tenant)

    report = link_attendance_devices(db, tenant, odoo, apply=True)
    assert report.linked == 1
    assert report.failures == ["GATE-01: no access to devices"]
    assert sorted(r["device_id"] is not None for r in odoo.attendances.values()) == [False, True]


# --------------------------------------------------------------------------- #
# The command-line tool
# --------------------------------------------------------------------------- #
def test_tool_reports_by_default_and_writes_with_apply(db, tenant, local_day, monkeypatch, capsys):
    odoo = FakeOdoo()
    run(db, tenant, odoo, _shift("1001", local_day), monkeypatch)
    _tracking(db, tenant)
    monkeypatch.setattr(tool_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(tool_mod, "build_odoo_client", lambda t, c: odoo)

    monkeypatch.setattr(sys, "argv", ["link_attendance_devices.py"])
    assert tool_mod.main() == 0
    out = capsys.readouterr().out
    assert "to link: 1 → GATE-01" in out and "run with --apply" in out
    assert all(r["device_id"] is None for r in odoo.attendances.values())

    monkeypatch.setattr(sys, "argv", ["link_attendance_devices.py", "--apply"])
    assert tool_mod.main() == 0
    assert "1 record(s) now point at their device" in capsys.readouterr().out
    assert all(r["device_id"] for r in odoo.attendances.values())


def test_tool_says_so_when_device_tracking_is_off(db, tenant, monkeypatch, capsys):
    monkeypatch.setattr(tool_mod, "SessionLocal", lambda: db)
    monkeypatch.setattr(sys, "argv", ["link_attendance_devices.py"])
    tool_mod.main()
    assert "device tracking is off" in capsys.readouterr().out
