"""A check-in that falls inside a record Odoo already has.

Odoo's hr.attendance refuses overlapping records, closed ones included — the
error ("the employee was already checked in on …") even prints the *new*
check-in. Retrying such a write can never work, so the engine must settle it:
shorten a record that BioBridge itself closed at the shift limit (a guess), or
mark the punch covered when a real record already spans it. FakeOdoo enforces
the same overlap rule and raises if the engine ever tries to write one.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.models import AttendanceRecord, PunchRecord, PunchState
from tests.conftest import FakeOdoo
from tests.test_odoo_close_crash_recovery import punch, run


def _first_open(db, tenant, odoo, local_day, monkeypatch):
    rows = [punch("in1", "1001", local_day.replace(hour=8), direction=True)]
    run(db, tenant, odoo, rows, monkeypatch)
    (att_id,) = odoo.attendances.keys()
    return att_id, rows


def _state(db, external_id):
    return db.scalars(select(PunchRecord).where(PunchRecord.external_id == external_id)).one()


def test_checkin_inside_a_record_closed_at_the_limit_shortens_it(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    rec = odoo.attendances[att_id]
    # What the stale-shift sweep does to a shift nobody closed.
    rec["check_out"] = rec["check_in"] + timedelta(hours=16)
    rec["auto_closed"] = True

    rows.append(punch("in2", "1001", local_day.replace(hour=10), direction=True))
    run(db, tenant, odoo, rows, monkeypatch)

    second = _state(db, "in2")
    assert second.process_state == PunchState.synced.value, second.error_message
    assert len(odoo.attendances) == 2
    new_id = second.odoo_attendance_id
    assert new_id != att_id
    # The guess is cut back to where the real punch begins; the new one is open.
    assert odoo.attendances[att_id]["check_out"] == odoo.attendances[new_id]["check_in"]
    assert odoo.attendances[new_id]["check_out"] is None


def test_the_shortened_record_is_mirrored_locally(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    rec = odoo.attendances[att_id]
    rec["check_out"] = rec["check_in"] + timedelta(hours=16)
    rows.append(punch("in2", "1001", local_day.replace(hour=10), direction=True))
    run(db, tenant, odoo, rows, monkeypatch)

    mirror = db.scalars(
        select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)
    ).one()
    assert mirror.check_out == odoo.attendances[att_id]["check_out"]
    assert mirror.is_auto_closed is True
    assert abs(mirror.worked_hours - 2.0) < 0.01


def test_checkin_inside_a_real_record_is_settled_not_retried(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    rec = odoo.attendances[att_id]
    rec["check_out"] = rec["check_in"] + timedelta(hours=3)      # a real, punched shift

    rows.append(punch("in2", "1001", local_day.replace(hour=9), direction=True))
    result = run(db, tenant, odoo, rows, monkeypatch)

    second = _state(db, "in2")
    assert second.process_state == PunchState.skipped.value
    assert str(att_id) in second.error_message and "overlapping" in second.error_message
    assert len(odoo.attendances) == 1                              # nothing written
    assert second.attempts == 0                                    # and not a failure to retry
    assert result.error_count == 0


def test_a_later_checkin_outside_any_record_is_unaffected(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    odoo.attendances[att_id]["check_out"] = odoo.attendances[att_id]["check_in"] + timedelta(hours=2)

    rows.append(punch("in2", "1001", local_day.replace(hour=14), direction=True))
    run(db, tenant, odoo, rows, monkeypatch)

    assert _state(db, "in2").process_state == PunchState.synced.value
    assert len(odoo.attendances) == 2


# --------------------------------------------------------------------------- #
# The real client's lookup: the same question Odoo's own constraint asks
# --------------------------------------------------------------------------- #
def _client_returning(rows):
    from tests.test_odoo_company_scoping import FakeXmlRpcModels, make_client

    client = make_client(FakeXmlRpcModels(), company_id=None)
    asked = []

    def execute(model, method, args, kwargs=None):
        if method == "search_read" and model == "hr.attendance":
            asked.append((args[0], kwargs))
            return rows
        return {"check_in": {}, "check_out": {}, "employee_id": {}} if method == "fields_get" else []

    client.execute = execute
    return client, asked


def test_covering_asks_for_the_latest_record_starting_at_or_before():
    from datetime import datetime

    client, asked = _client_returning([
        {"id": 5, "check_in": "2026-10-16 12:05:10", "check_out": "2026-10-17 04:05:10"}
    ])
    moment = datetime(2026, 10, 16, 12, 25, 10)
    got = client.attendance_covering(1, moment)

    domain, kwargs = asked[0]
    assert ("employee_id", "=", 1) in domain
    assert any(c[0] == "check_in" and c[1] == "<=" for c in domain)
    assert kwargs["order"] == "check_in desc" and kwargs["limit"] == 1
    assert got["id"] == 5


def test_covering_is_none_when_that_record_ended_before_the_moment():
    from datetime import datetime

    client, _ = _client_returning([
        {"id": 5, "check_in": "2026-10-16 08:00:00", "check_out": "2026-10-16 10:00:00"}
    ])
    assert client.attendance_covering(1, datetime(2026, 10, 16, 12, 0, 0)) is None


def test_covering_returns_the_record_when_it_ends_after_the_moment():
    from datetime import datetime

    client, _ = _client_returning([
        {"id": 5, "check_in": "2026-10-16 12:05:10", "check_out": "2026-10-17 04:05:10"}
    ])
    assert client.attendance_covering(1, datetime(2026, 10, 16, 12, 25, 10))["id"] == 5


def test_covering_ignores_an_open_record():
    from datetime import datetime

    client, _ = _client_returning([
        {"id": 5, "check_in": "2026-10-16 08:00:00", "check_out": False}
    ])
    assert client.attendance_covering(1, datetime(2026, 10, 16, 12, 0, 0)) is None


# --------------------------------------------------------------------------- #
# A record closed in Odoo behind BioBridge's back must stop showing as Open
# --------------------------------------------------------------------------- #
def test_a_record_closed_in_odoo_is_closed_here_too(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)).one()
    assert mirror.check_out is None

    rec = odoo.attendances[att_id]
    rec["check_out"] = rec["check_in"] + timedelta(hours=16)       # the sweep, or someone by hand
    run(db, tenant, odoo, rows, monkeypatch)

    db.refresh(mirror)
    assert mirror.check_out == rec["check_out"]
    assert abs(mirror.worked_hours - 16.0) < 0.01
    assert mirror.is_auto_closed is True


def test_a_record_still_open_in_odoo_stays_open_here(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    run(db, tenant, odoo, rows, monkeypatch)
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)).one()
    assert mirror.check_out is None


# --------------------------------------------------------------------------- #
# The hourly stale-shift sweep must update BioBridge's own copy, not only Odoo
# --------------------------------------------------------------------------- #
def test_stale_sweep_closes_the_local_record_too(db, tenant, local_day, monkeypatch):
    from app.services import sync_engine as engine_mod

    odoo = FakeOdoo()
    rows = [punch("in1", "1001", local_day.replace(hour=1), direction=True)]
    run(db, tenant, odoo, rows, monkeypatch)
    (att_id,) = odoo.attendances.keys()
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)).one()
    assert mirror.check_out is None

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    assert engine_mod.close_stale_attendances(db, tenant) == 1

    # Odoo and the local copy agree immediately — no later sync needed.
    db.refresh(mirror)
    assert odoo.attendances[att_id]["check_out"] is not None
    assert mirror.check_out == odoo.attendances[att_id]["check_out"]
    assert mirror.is_auto_closed is True
    assert abs(mirror.worked_hours - tenant.max_shift_hours) < 0.01
