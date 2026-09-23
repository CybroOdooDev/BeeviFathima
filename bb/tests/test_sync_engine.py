"""The engine end to end, against a stubbed Odoo that enforces real constraints."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.integrations.base import EmployeeRecord
from app.models import (
    AttendanceRecord,
    DeviceSource,
    EmployeeMapping,
    MappingStatus,
    OdooConnection,
    PunchRecord,
    PunchState,
    SubscriptionPlan,
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


def test_plan_cap_blocks_new_matches_once_reached(db, tenant, local_day, monkeypatch):
    """A plan's employee cap holds back new matches once it is reached.

    Codes are matched in sorted order (see _resolve_mappings), so with a cap
    of one and two badges arriving in the same run, "1001" fills the seat and
    "1002" is held back — not matched, not even looked up in Odoo — with a
    note that says why rather than the usual "no such employee" message.
    """
    plan = SubscriptionPlan(name="Capped", max_employees=1)
    db.add(plan)
    db.flush()
    tenant.plan_id = plan.id
    db.commit()

    odoo = FakeOdoo()  # 1001 -> Jane Doe, 1002 -> Omar Haddad, both known to Odoo
    rows = [
        punch(1, "1001", local_day.replace(hour=8)),
        punch(2, "1002", local_day.replace(hour=8, minute=5)),
    ]
    result = run(db, tenant, odoo, rows, monkeypatch)
    assert result.status == "success", result.error_message

    mappings = {m.emp_code: m for m in db.scalars(select(EmployeeMapping)).all()}
    assert mappings["1001"].status == MappingStatus.mapped.value
    assert mappings["1002"].status == MappingStatus.unmapped.value
    assert "plan allows up to 1" in mappings["1002"].match_note
    assert mappings["1002"].odoo_employee_id is None, "held back before ever reaching Odoo"


def test_plan_cap_does_not_unmap_existing_matches(db, tenant, local_day, monkeypatch):
    """Lowering a tenant's cap after the fact must not undo who already fits.

    A plan can be assigned — or downgraded — after a tenant already has more
    mapped employees than its new cap allows. Enforcement only ever holds
    back *new* matches (see SubscriptionPlan.max_employees); it must never
    walk back a match that already exists, or a billing change would break
    live attendance for someone it never touched.
    """
    odoo = FakeOdoo()  # 1001, 1002 both known, no cap yet
    rows = [
        punch(1, "1001", local_day.replace(hour=8)),
        punch(2, "1002", local_day.replace(hour=8, minute=5)),
    ]
    run(db, tenant, odoo, rows, monkeypatch)
    mapped_before = {
        m.emp_code for m in db.scalars(select(EmployeeMapping)).all()
        if m.status == MappingStatus.mapped.value
    }
    assert mapped_before == {"1001", "1002"}

    # A plan is assigned afterward, with a cap already below what is in use.
    plan = SubscriptionPlan(name="Capped", max_employees=1)
    db.add(plan)
    db.flush()
    tenant.plan_id = plan.id
    db.commit()

    odoo.employees["1003"] = (13, "New Hire")
    rows.append(punch(3, "1003", local_day.replace(hour=8, minute=10)))
    result = run(db, tenant, odoo, rows, monkeypatch)
    assert result.status == "success", result.error_message

    mappings = {m.emp_code: m for m in db.scalars(select(EmployeeMapping)).all()}
    assert mappings["1001"].status == MappingStatus.mapped.value, "already-matched, left alone"
    assert mappings["1002"].status == MappingStatus.mapped.value, "already-matched, left alone"
    assert mappings["1003"].status == MappingStatus.unmapped.value, "new badge, held back by the cap"
    assert "plan allows up to 1" in mappings["1003"].match_note


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


def test_each_punch_is_stamped_with_the_run_that_ingested_it(
    db, tenant, local_day, monkeypatch
):
    """"What did this sync bring in" has to be answerable from the ledger.

    The run counters give a number; only this stamp gives the rows, which is
    what anyone actually wants when a run looks wrong.
    """
    odoo = FakeOdoo()
    rows = [
        punch(1, "1001", local_day.replace(hour=8)),
        punch(2, "1001", local_day.replace(hour=17)),
    ]
    first = run(db, tenant, odoo, rows, monkeypatch)

    stored = db.scalars(select(PunchRecord)).all()
    assert len(stored) == 2
    assert {p.first_seen_run_id for p in stored} == {first.id}


def test_a_re_read_punch_stays_with_the_run_that_first_saw_it(
    db, tenant, local_day, monkeypatch
):
    """Every cycle re-reads a window of known punches on purpose, so the same
    punch is *fetched* by several runs. Re-attributing it each time would make
    the earlier run's history change under you."""
    odoo = FakeOdoo()
    rows = [
        punch(1, "1001", local_day.replace(hour=8)),
        punch(2, "1001", local_day.replace(hour=17)),
    ]
    first = run(db, tenant, odoo, rows, monkeypatch)

    # The same two punches, plus one more, offered again.
    rows.append(punch(3, "1001", local_day.replace(hour=18)))
    second = run(db, tenant, odoo, rows, monkeypatch)

    by_external = {p.external_id: p.first_seen_run_id for p in db.scalars(select(PunchRecord))}
    assert by_external["1"] == first.id, "re-reading must not move it to the later run"
    assert by_external["2"] == first.id
    assert by_external["3"] == second.id
    assert second.punches_new == 1, "only the new one counts as new"


# --------------------------------------------------------------------------- #
# Companion add-on: device tracking (odoo_addon/biobridge_attendance/)
# --------------------------------------------------------------------------- #
def test_device_is_registered_and_linked_when_the_addon_is_present(
    db, tenant, local_day, monkeypatch
):
    """With has_device_tracking on, the terminal a shift's punches came from
    is upserted into Odoo's biobridge.device and its id lands on the
    attendance record — end to end, this is the feature the fixture's own
    Device('GATE-01') exists for. One terminal closing two different
    employees' shifts in the same cycle must still upsert it only once."""
    conn = db.scalar(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id))
    conn.has_device_tracking = True
    db.commit()

    odoo = FakeOdoo()
    rows = [
        punch(1, "1001", local_day.replace(hour=8)),
        punch(2, "1001", local_day.replace(hour=17)),
        punch(3, "1002", local_day.replace(hour=8)),
        punch(4, "1002", local_day.replace(hour=17)),
    ]
    run(db, tenant, odoo, rows, monkeypatch)

    assert len(odoo.attendances) == 2
    assert odoo.devices == {"GATE-01": 501}
    assert all(r["device_id"] == 501 for r in odoo.attendances.values())
    assert odoo.calls.count("upsert_device:GATE-01") == 1, "cached, not upserted per interval"


def test_device_id_omitted_when_the_addon_is_absent(db, tenant, local_day, monkeypatch):
    """The default, unflagged connection: no upsert call, no device_id sent —
    this is what keeps a plain Odoo (no biobridge_attendance installed)
    working exactly as before this feature existed."""
    odoo = FakeOdoo()
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)

    assert odoo.devices == {}
    assert not any(c.startswith("upsert_device") for c in odoo.calls)
    record = next(iter(odoo.attendances.values()))
    assert record["device_id"] is None


# --------------------------------------------------------------------------- #
# Roster provisioning: Odoo's employees -> the provider (auto_provision_employees)
# --------------------------------------------------------------------------- #
def _provisioning_source(db, tenant) -> DeviceSource:
    source = db.scalar(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id))
    source.auto_provision_employees = True
    db.commit()
    return source


def test_provisions_active_odoo_employees_missing_on_the_provider(
    db, tenant, monkeypatch
):
    _provisioning_source(db, tenant)

    odoo = FakeOdoo()
    odoo.roster = [
        {"id": 1, "name": "New Hire", "barcode": "9001", "active": True},
    ]
    provider = FakeProvider([])
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)

    result = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert [e.emp_code for e in provider.created_employees] == ["9001"]
    assert provider.created_employees[0].first_name == "New"
    assert provider.created_employees[0].last_name == "Hire"
    assert result.employees_provisioned == 1


def test_inactive_odoo_employees_are_not_provisioned(db, tenant, monkeypatch):
    _provisioning_source(db, tenant)

    odoo = FakeOdoo()
    odoo.roster = [{"id": 1, "name": "Left The Company", "barcode": "9002", "active": False}]
    provider = FakeProvider([])
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)

    result = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert provider.created_employees == []
    assert result.employees_provisioned == 0


def test_employees_already_on_the_provider_are_not_recreated(db, tenant, monkeypatch):
    _provisioning_source(db, tenant)

    odoo = FakeOdoo()
    odoo.roster = [{"id": 1, "name": "Already There", "barcode": "9003", "active": True}]
    existing = EmployeeRecord(external_id="1", emp_code="9003", first_name="Already", last_name="There")
    provider = FakeProvider([], employees=[existing])
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)

    engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert provider.created_employees == []


def test_provisioning_is_off_by_default(db, tenant, local_day, monkeypatch):
    """The flag has to be turned on per source — this is the fixture's
    DeviceSource with auto_provision_employees left at its default."""
    odoo = FakeOdoo()
    odoo.roster = [{"id": 1, "name": "New Hire", "barcode": "9004", "active": True}]
    provider = FakeProvider([punch(1, "1001", local_day.replace(hour=8))])
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)

    result = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert provider.created_employees == []
    assert result.employees_provisioned == 0


def test_provisioning_skips_a_provider_with_no_employee_capability(db, tenant, monkeypatch):
    _provisioning_source(db, tenant)

    odoo = FakeOdoo()
    odoo.roster = [{"id": 1, "name": "New Hire", "barcode": "9005", "active": True}]
    provider = FakeProvider([], supports_employees=False)
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)

    result = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert provider.created_employees == []
    assert result.employees_provisioned == 0
    assert result.status != "failed", "an unsupported capability must not fail the run"


def test_a_bad_odoo_roster_read_does_not_fail_the_run(db, tenant, local_day, monkeypatch):
    """The point of running this before mapping, not depending on it: a
    roster-read failure is logged and skipped, not fatal — punches already
    captured this cycle still get ingested and pushed."""
    from app.integrations.odoo import OdooError

    _provisioning_source(db, tenant)

    class BrokenRosterOdoo(FakeOdoo):
        def list_employees(self, limit=0):
            raise OdooError("temporary Odoo hiccup")

    odoo = BrokenRosterOdoo()
    provider = FakeProvider([punch(1, "1001", local_day.replace(hour=8))])
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)

    result = engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    assert result.employees_provisioned == 0
    assert len(odoo.attendances) == 1, "the rest of the cycle still ran"
