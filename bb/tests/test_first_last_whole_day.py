"""First/last pairing is one record per shift day, however many sync cycles the
day's punches arrive over.

Before: each cycle paired only the punches that were still pending, so every
cycle with activity opened its own record. Now the day's already-synced punches
are weighed too, and the day's one record is updated in place.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.models import PunchRecord, PunchState
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo, FakeProvider


def punch(pid, emp, moment):
    return {
        "id": pid, "emp_code": emp,
        "punch_time": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "direction": None, "terminal_sn": "GATE-01",
        "first_name": "X", "last_name": "Y",
    }


def cycle(db, tenant, odoo, rows, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider(rows))
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def _first_last(db, tenant):
    tenant.pairing_mode = "first_last"
    db.commit()


def test_later_cycles_update_the_days_record(db, tenant, local_day, monkeypatch):
    _first_last(db, tenant)
    odoo = FakeOdoo()
    d = local_day
    rows = [punch("a1", "1001", d.replace(hour=8, minute=0)),
            punch("a2", "1001", d.replace(hour=8, minute=10))]
    cycle(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 1

    rows += [punch("a3", "1001", d.replace(hour=9, minute=0)),
             punch("a4", "1001", d.replace(hour=9, minute=10))]
    cycle(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 1
    rec = next(iter(odoo.attendances.values()))
    assert rec["check_out"] - rec["check_in"] == timedelta(hours=1, minutes=10)

    rows += [punch("a5", "1001", d.replace(hour=17, minute=0))]
    cycle(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 1
    rec = next(iter(odoo.attendances.values()))
    assert rec["check_out"] - rec["check_in"] == timedelta(hours=9)


def test_late_arriving_earlier_punch_moves_check_in(db, tenant, local_day, monkeypatch):
    _first_last(db, tenant)
    odoo = FakeOdoo()
    d = local_day
    rows = [punch("b1", "1001", d.replace(hour=9, minute=0)),
            punch("b2", "1001", d.replace(hour=17, minute=0))]
    cycle(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 1

    # The ingest cursor means a provider never hands back an earlier punch, but a
    # held / retried one can still come through pending: stage one directly.
    b1 = db.scalars(select(PunchRecord).where(PunchRecord.external_id == "b1")).one()
    late = PunchRecord(
        tenant_id=b1.tenant_id, source_id=b1.source_id, external_id="b0",
        emp_code=b1.emp_code, punch_time_utc=b1.punch_time_utc.replace(hour=3, minute=30),
        direction=b1.direction, terminal_sn=b1.terminal_sn, device_id=b1.device_id,
        process_state=PunchState.pending.value,
    )
    db.add(late)
    db.commit()
    cycle(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 1
    rec = next(iter(odoo.attendances.values()))
    assert rec["check_out"] - rec["check_in"] == timedelta(hours=9, minutes=30)


def test_middle_punches_do_not_linger_as_pending(db, tenant, local_day, monkeypatch):
    _first_last(db, tenant)
    odoo = FakeOdoo()
    d = local_day
    rows = [punch(f"m{i}", "1001", d.replace(hour=8 + i)) for i in range(5)]
    cycle(db, tenant, odoo, rows, monkeypatch)
    states = {p.external_id: p.process_state for p in db.scalars(select(PunchRecord))}
    assert set(states.values()) == {PunchState.synced.value}
    assert len(odoo.attendances) == 1


def test_next_day_gets_its_own_record(db, tenant, local_day, monkeypatch):
    _first_last(db, tenant)
    odoo = FakeOdoo()
    d = local_day
    rows = [punch("c1", "1001", d.replace(hour=8)), punch("c2", "1001", d.replace(hour=17))]
    cycle(db, tenant, odoo, rows, monkeypatch)
    tomorrow = d + timedelta(days=1)
    rows += [punch("c3", "1001", tomorrow.replace(hour=8)), punch("c4", "1001", tomorrow.replace(hour=17))]
    cycle(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 2


def test_days_are_grouped_by_local_time_not_utc():
    from datetime import datetime

    from app.services.pairing import PairingConfig, PairingMode, Punch, pair_punches

    # 08:00 and 17:00 in Asia/Kolkata are 02:30 and 11:30 UTC — either side of a
    # 04:00 UTC day boundary, but the same local working day.
    punches = [
        Punch("p1", "1001", datetime(2026, 10, 8, 2, 30)),
        Punch("p2", "1001", datetime(2026, 10, 8, 11, 30)),
    ]
    cfg = PairingConfig(mode=PairingMode.first_last, timezone="Asia/Kolkata")
    out = pair_punches(punches, cfg).intervals
    assert len(out) == 1
    assert out[0].check_in == datetime(2026, 10, 8, 2, 30)
    assert out[0].check_out == datetime(2026, 10, 8, 11, 30)


def test_records_are_stamped_with_the_method_that_wrote_them(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    d = local_day
    tenant.pairing_mode = "alternating"
    db.commit()
    rows = [punch("s1", "1001", d.replace(hour=8)), punch("s2", "1001", d.replace(hour=8, minute=30))]
    cycle(db, tenant, odoo, rows, monkeypatch)
    tenant.pairing_mode = "first_last"
    db.commit()
    rows += [punch("s3", "1002", d.replace(hour=9)), punch("s4", "1002", d.replace(hour=17))]
    cycle(db, tenant, odoo, rows, monkeypatch)
    tags = sorted(r["pairing_mode"] for r in odoo.attendances.values())
    assert tags == ["alternating", "first_last"]
    from app.models import AttendanceRecord
    local = sorted(r.pairing_mode for r in db.scalars(select(AttendanceRecord)))
    assert local == ["alternating", "first_last"]
