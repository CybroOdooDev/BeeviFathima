"""Does a worker crash after Odoo creation, but before the local commit,
produce a duplicate attendance record on the next run?

This isn't inferred from reading the code — it's simulated directly: run a
cycle so a punch gets pushed and an Odoo record created, then roll the local
punch state back to exactly what it would be if the process had died right
after the Odoo write landed but before `db.commit()` ran (the punch is still
`pending`, `odoo_attendance_id` is still unset). Then run again and check
whether Odoo ends up with one record or two.

FakeOdoo (tests/conftest.py) enforces the real Odoo constraint that an
employee cannot have two open attendance records at once, and raises loudly
if the engine ever tries — so this is a strict check, not just a count.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import AttendanceRecord, PunchRecord, PunchState
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo, FakeProvider


def punch(pid, emp, moment, direction=None):
    return {
        "id": pid,
        "emp_code": emp,
        "punch_time": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "direction": direction,
        "terminal_sn": "GATE-01",
        "first_name": "X",
        "last_name": "Y",
    }


def run(db, tenant, odoo, rows, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider(rows))
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def test_crash_after_odoo_write_does_not_duplicate(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    check_in = local_day.replace(hour=8)

    run(db, tenant, odoo, [punch("p1", "1001", check_in, direction=True)], monkeypatch)

    row = db.scalars(select(PunchRecord).where(PunchRecord.external_id == "p1")).one()
    assert row.process_state == PunchState.synced.value
    real_attendance_id = row.odoo_attendance_id
    assert real_attendance_id is not None
    assert len(odoo.attendances) == 1

    row.process_state = PunchState.pending.value
    row.odoo_attendance_id = None
    db.commit()

    run(db, tenant, odoo, [punch("p1", "1001", check_in, direction=True)], monkeypatch)

    assert len(odoo.attendances) == 1
    row2 = db.scalars(select(PunchRecord).where(PunchRecord.external_id == "p1")).one()
    assert row2.process_state == PunchState.synced.value
    assert row2.odoo_attendance_id == real_attendance_id


def _crash_after_close_scenario(db, tenant, odoo, local_day, monkeypatch, pairing_mode):
    tenant.pairing_mode = pairing_mode
    db.commit()

    check_in = local_day.replace(hour=8)
    check_out = local_day.replace(hour=17)

    run(db, tenant, odoo, [punch("in1", "1001", check_in, direction=True)], monkeypatch)
    open_id = list(odoo.attendances.keys())[0]
    real_check_in = odoo.attendances[open_id]["check_in"]
    assert odoo.attendances[open_id]["check_out"] is None

    run(db, tenant, odoo, [
        punch("in1", "1001", check_in, direction=True),
        punch("out1", "1001", check_out, direction=False),
    ], monkeypatch)
    assert odoo.attendances[open_id]["check_out"] is not None

    out_row = db.scalars(select(PunchRecord).where(PunchRecord.external_id == "out1")).one()
    assert out_row.process_state == PunchState.synced.value

    # Simulate the crash: local commit for the close never landed.
    out_row.process_state = PunchState.pending.value
    out_row.odoo_attendance_id = None
    db.commit()
    calls_before_retry = len(odoo.calls)

    run(db, tenant, odoo, [
        punch("in1", "1001", check_in, direction=True),
        punch("out1", "1001", check_out, direction=False),
    ], monkeypatch)

    assert len(odoo.attendances) == 1, (
        f"[{pairing_mode}] must not have created a second record: {odoo.attendances}"
    )
    assert odoo.attendances[open_id]["check_out"] is not None
    assert odoo.attendances[open_id]["check_in"] == real_check_in, (
        f"[{pairing_mode}] the real record's check_in must not have been touched"
    )
    new_calls = odoo.calls[calls_before_retry:]
    assert not any(c.startswith("create") for c in new_calls), (
        f"[{pairing_mode}] retry must not re-create; calls were: {new_calls}"
    )

    # The local mirror must reflect the REAL shift, not the misread one.
    mirror = db.scalars(
        select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == open_id)
    ).one()
    assert mirror.check_in == real_check_in, (
        f"[{pairing_mode}] local mirror's check_in got corrupted: {mirror.check_in} "
        f"!= real {real_check_in}"
    )
    assert mirror.check_out == odoo.attendances[open_id]["check_out"]
    print(f"[{pairing_mode}] retry calls after simulated crash: {new_calls}")


def test_crash_after_close_alternating_mode(db, tenant, local_day, monkeypatch):
    _crash_after_close_scenario(db, tenant, FakeOdoo(), local_day, monkeypatch, "alternating")


def test_crash_after_close_state_based_mode(db, tenant, local_day, monkeypatch):
    _crash_after_close_scenario(db, tenant, FakeOdoo(), local_day, monkeypatch, "state_based")


def test_crash_after_close_first_last_mode(db, tenant, local_day, monkeypatch):
    _crash_after_close_scenario(db, tenant, FakeOdoo(), local_day, monkeypatch, "first_last")


def test_a_genuine_new_shift_is_never_suppressed(db, tenant, local_day, monkeypatch):
    """The fix must not treat every check-in-direction-mismatch as recovery.

    A punch recorded with direction "out" that does NOT correspond to any
    already-closed Odoo record is a real orphan check-out (or, in
    alternating mode, a real day's only punch) — it must still be created
    normally, not silently dropped.
    """
    odoo = FakeOdoo()
    lone_checkout = local_day.replace(hour=9)

    run(db, tenant, odoo, [punch("solo", "1001", lone_checkout, direction=False)], monkeypatch)

    # Something must have been written -- either a flagged orphan record
    # (state_based) or an open interval (alternating/first_last) -- not
    # silently nothing.
    assert len(odoo.attendances) == 1, odoo.attendances
    row = db.scalars(select(PunchRecord).where(PunchRecord.external_id == "solo")).one()
    assert row.process_state == PunchState.synced.value
