"""The engine end to end, against a stubbed Odoo that enforces real constraints."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AttendanceRecord,
    EmployeeMapping,
    MappingStatus,
    PunchRecord,
    PunchState,
)
from app.services import sync_engine as engine_mod
from tests.conftest import TZ, FakeOdoo, FakeProvider

DUBAI_OFFSET = timedelta(hours=4)


def run(db, tenant, odoo, rows, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider(rows))
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


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


# --------------------------------------------------------------------------- #
# The case the whole design turns on
# --------------------------------------------------------------------------- #
def test_checkout_in_a_later_cycle_closes_the_same_record(db, tenant, local_day, monkeypatch):
    """Check-in in cycle 1, check-out in cycle 2 — the normal polling case."""
    odoo = FakeOdoo()
    check_in = local_day.replace(hour=8)
    check_out = local_day.replace(hour=17)

    run1 = run(db, tenant, odoo, [punch(1, "1001", check_in)], monkeypatch)
    assert run1.status == "success", run1.error_message
    assert len(odoo.attendances) == 1
    opened = next(iter(odoo.attendances.values()))
    assert opened["check_out"] is None
    assert opened["check_in"] == check_in - DUBAI_OFFSET, "stored as UTC, not wall-clock"

    run2 = run(
        db, tenant, odoo,
        [punch(1, "1001", check_in), punch(2, "1001", check_out)],
        monkeypatch,
    )
    assert run2.status == "success", run2.error_message

    assert len(odoo.attendances) == 1, "no phantom second record"
    record = next(iter(odoo.attendances.values()))
    assert record["check_in"] == check_in - DUBAI_OFFSET
    assert record["check_out"] == check_out - DUBAI_OFFSET
    assert not [r for r in odoo.attendances.values() if r["check_out"] is None]


def test_three_cycles_one_shift_stays_one_record(db, tenant, local_day, monkeypatch):
    """A third empty cycle must not disturb what the first two settled."""
    odoo = FakeOdoo()
    rows = [
        punch(1, "1001", local_day.replace(hour=8)),
        punch(2, "1001", local_day.replace(hour=17)),
    ]
    run(db, tenant, odoo, rows[:1], monkeypatch)
    run(db, tenant, odoo, rows, monkeypatch)
    run(db, tenant, odoo, rows, monkeypatch)

    assert len(odoo.attendances) == 1
    assert db.scalar(select(AttendanceRecord.worked_hours)) == pytest.approx(9.0)


# --------------------------------------------------------------------------- #
# Ingest and idempotency
# --------------------------------------------------------------------------- #
def test_all_punches_ingested_once(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    rows = [
        punch(1, "1001", local_day.replace(hour=8)),
        punch(2, "1001", local_day.replace(hour=12)),
        punch(3, "1001", local_day.replace(hour=13)),
        punch(4, "1001", local_day.replace(hour=17)),
    ]
    first = run(db, tenant, odoo, rows, monkeypatch)
    assert first.punches_new == 4

    second = run(db, tenant, odoo, rows, monkeypatch)
    assert second.punches_new == 0, "re-reading the window must ingest nothing new"
    assert db.scalar(select(AttendanceRecord.worked_hours).limit(1)) is not None
    assert len(db.scalars(select(PunchRecord)).all()) == 4


def test_double_tap_is_skipped(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    rows = [
        punch(1, "1001", local_day.replace(hour=8, minute=0)),
        punch(2, "1001", local_day.replace(hour=8, minute=0, second=20)),
        punch(3, "1001", local_day.replace(hour=17)),
    ]
    run(db, tenant, odoo, rows, monkeypatch)

    skipped = db.scalars(
        select(PunchRecord).where(PunchRecord.process_state == PunchState.skipped.value)
    ).all()
    assert len(skipped) == 1
    assert "Duplicate punch" in skipped[0].error_message


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #
def test_unknown_badge_parks_instead_of_failing_the_run(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    rows = [punch(1, "9999", local_day.replace(hour=8))]
    result = run(db, tenant, odoo, rows, monkeypatch)

    assert result.status == "success", "an unmatched badge is not a failed run"
    mapping = db.scalars(
        select(EmployeeMapping).where(EmployeeMapping.emp_code == "9999")
    ).first()
    assert mapping.status == MappingStatus.unmapped.value
    assert "No Odoo employee" in mapping.match_note
    assert odoo.attendances == {}

    ledger = db.scalars(select(PunchRecord)).first()
    assert ledger.process_state == PunchState.unmapped.value


def test_a_badge_matched_later_gets_its_punches_pushed(db, tenant, local_day, monkeypatch):
    """The punch is held, not lost, until the employee exists in Odoo."""
    odoo = FakeOdoo(employees={})
    rows = [punch(1, "1001", local_day.replace(hour=8))]
    run(db, tenant, odoo, rows, monkeypatch)
    assert odoo.attendances == {}

    odoo.employees["1001"] = (11, "Jane Doe")
    run(db, tenant, odoo, rows, monkeypatch)

    assert len(odoo.attendances) == 1
    assert db.scalars(select(PunchRecord)).first().process_state == PunchState.synced.value


def test_badges_register_even_when_odoo_is_absent(db, tenant, local_day, monkeypatch):
    """The Employees page must not look empty while punches pile up."""
    from app.models import OdooConnection

    conn = db.scalars(select(OdooConnection)).first()
    conn.is_active = False
    db.commit()

    result = run(db, tenant, FakeOdoo(), [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)

    assert result.status == "failed"
    assert "no active Odoo connection" in result.error_message
    assert db.scalars(select(EmployeeMapping)).all(), "the badge was still registered"
    assert db.scalars(select(PunchRecord)).all(), "and the punch was still captured"


# --------------------------------------------------------------------------- #
# Cursor and failure handling
# --------------------------------------------------------------------------- #
def test_cursor_does_not_advance_when_the_provider_fails(db, tenant, local_day, monkeypatch):
    from app.integrations.base import ProviderError
    from app.models import DeviceSource

    class Broken:
        label = "Broken"
        cached_token = None

        def fetch_punches(self, since=None, until=None):
            raise ProviderError("BioTime is down")
            yield  # pragma: no cover

        def close(self):
            pass

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: Broken())
    result = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert result.status == "failed"
    source = db.scalars(select(DeviceSource)).first()
    assert source.cursor_punch_time is None, "cursor must not move past unread punches"
    assert source.status == "failed"
    assert "BioTime is down" in source.status_message


def test_cursor_advances_to_the_newest_punch(db, tenant, local_day, monkeypatch):
    from app.models import DeviceSource

    odoo = FakeOdoo()
    newest = local_day.replace(hour=17)
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8)), punch(2, "1001", newest)], monkeypatch)

    source = db.scalars(select(DeviceSource)).first()
    assert source.cursor_punch_time == newest - DUBAI_OFFSET


def test_a_configuration_problem_does_not_count_as_a_failure(db, tenant, monkeypatch):
    """SyncAborted must not push the tenant towards the slow lane."""
    from app.models import DeviceSource

    for source in db.scalars(select(DeviceSource)).all():
        source.is_active = False
    db.commit()

    result = engine_mod.SyncEngine(db, tenant, "test").run_cycle()
    assert result.status == "failed"
    assert tenant.consecutive_failures == 0


# --------------------------------------------------------------------------- #
# Mirror
# --------------------------------------------------------------------------- #
def test_local_mirror_matches_what_was_pushed(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    rows = [
        punch(1, "1001", local_day.replace(hour=8)),
        punch(2, "1001", local_day.replace(hour=17)),
    ]
    run(db, tenant, odoo, rows, monkeypatch)

    record = db.scalars(select(AttendanceRecord)).first()
    assert record.odoo_attendance_id in odoo.attendances
    assert record.worked_hours == pytest.approx(9.0)
    assert record.check_in_local == local_day.replace(hour=8), "local column is wall-clock"
    assert record.check_in == local_day.replace(hour=8) - DUBAI_OFFSET


def test_late_arrival_scored_once_per_day(db, tenant, local_day, monkeypatch):
    """Coming back from lunch must not read as hours late."""
    odoo = FakeOdoo()
    rows = [
        punch(1, "1001", local_day.replace(hour=9, minute=30)),   # 60 min late
        punch(2, "1001", local_day.replace(hour=12)),
        punch(3, "1001", local_day.replace(hour=13)),             # not a late arrival
        punch(4, "1001", local_day.replace(hour=17)),
    ]
    run(db, tenant, odoo, rows, monkeypatch)

    records = db.scalars(select(AttendanceRecord).order_by(AttendanceRecord.check_in)).all()
    assert len(records) == 2
    assert records[0].is_late is True and records[0].late_minutes == 60
    assert records[1].is_late is False
