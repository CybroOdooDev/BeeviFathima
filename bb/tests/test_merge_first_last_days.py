from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime

from sqlalchemy import select

from app.models import AttendanceRecord
from tests.conftest import FakeOdoo
from tests.test_first_last_whole_day import cycle, punch

spec = importlib.util.spec_from_file_location(
    "merge_tool", pathlib.Path(__file__).parent.parent / "tools" / "merge_first_last_days.py"
)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class _Odoo(FakeOdoo):
    clash = None

    def execute(self, model, method, args, kwargs=None):
        if method == "search_read":
            return self.clash or []
        assert (model, method) == ("hr.attendance", "unlink")
        for i in args[0]:
            self.attendances.pop(i)
        return True


def _as_first_last(db, odoo):
    """Make today's alternating-built records look like the old first/last
    duplicates: written (and recorded) under first/last."""
    for r in db.scalars(select(AttendanceRecord)).all():
        r.pairing_mode = "first_last"
    for rec in odoo.attendances.values():
        rec["pairing_mode"] = "first_last"
    db.commit()


def test_merges_old_duplicates_into_one(db, tenant, local_day, monkeypatch):
    # Old behaviour: alternating pairing leaves one record per in/out pair.
    tenant.pairing_mode = "alternating"
    db.commit()
    odoo = _Odoo()
    d = local_day
    rows = []
    for i, h in enumerate((8, 10, 12)):
        rows += [punch(f"x{i}a", "1001", d.replace(hour=h)),
                 punch(f"x{i}b", "1001", d.replace(hour=h, minute=30))]
        cycle(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 3

    tenant.pairing_mode = "first_last"
    db.commit()
    _as_first_last(db, odoo)
    plan = tool.plan_for(db, tenant, None)
    assert len(plan) == 1 and len(plan[0]["drop"]) == 2

    tool.apply_plan(db, tenant, odoo, plan[0])
    assert len(odoo.attendances) == 1
    rec = next(iter(odoo.attendances.values()))
    assert rec["check_in"].hour == 4 and rec["check_out"].minute == 30  # 08:00 .. 12:30 local (UTC+4)
    mirror = db.scalars(select(AttendanceRecord)).all()
    assert len(mirror) == 1
    assert mirror[0].check_out == rec["check_out"]


def test_skips_a_day_that_would_overlap_another_record(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "alternating"
    db.commit()
    odoo = _Odoo()
    d = local_day
    rows = []
    for i, h in enumerate((8, 10)):
        rows += [punch(f"y{i}a", "1001", d.replace(hour=h)),
                 punch(f"y{i}b", "1001", d.replace(hour=h, minute=30))]
        cycle(db, tenant, odoo, rows, monkeypatch)
    tenant.pairing_mode = "first_last"
    db.commit()
    _as_first_last(db, odoo)
    odoo.clash = [{"id": 9, "check_in": "2026-10-08 09:00:00", "check_out": False}]
    plan = tool.plan_for(db, tenant, None)
    try:
        tool.apply_plan(db, tenant, odoo, plan[0])
        raise AssertionError("should have been skipped")
    except tool.Skipped:
        pass
    assert len(odoo.attendances) == 2  # nothing deleted


def test_open_last_record_leaves_the_day_open(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "alternating"
    db.commit()
    odoo = _Odoo()
    d = local_day
    rows = [punch("z0a", "1001", d.replace(hour=8)), punch("z0b", "1001", d.replace(hour=8, minute=30)),
            punch("z1a", "1001", d.replace(hour=10))]
    for n in (2, 3):
        cycle(db, tenant, odoo, rows[:n], monkeypatch)
    assert sum(1 for r in odoo.attendances.values() if r["check_out"] is None) == 1
    tenant.pairing_mode = "first_last"
    db.commit()
    _as_first_last(db, odoo)
    plan = tool.plan_for(db, tenant, None)
    assert plan[0]["check_out"] is None
    tool.apply_plan(db, tenant, odoo, plan[0])
    assert len(odoo.attendances) == 1
    assert next(iter(odoo.attendances.values()))["check_out"] is None


def test_days_mixing_pairing_methods_are_left_alone(db, tenant, local_day, monkeypatch):
    # An account that switched method part-way through: the day's earlier
    # records were written under alternating, the later under first/last.
    tenant.pairing_mode = "alternating"
    db.commit()
    odoo = _Odoo()
    d = local_day
    rows = []
    for i, h in enumerate((8, 10)):
        rows += [punch(f"w{i}a", "1001", d.replace(hour=h)),
                 punch(f"w{i}b", "1001", d.replace(hour=h, minute=30))]
        cycle(db, tenant, odoo, rows, monkeypatch)
    tenant.pairing_mode = "first_last"
    db.commit()
    recs = db.scalars(select(AttendanceRecord).order_by(AttendanceRecord.check_in)).all()
    recs[1].pairing_mode = "first_last"
    db.commit()
    skipped = []
    assert tool.plan_for(db, tenant, None, skipped) == []
    assert len(skipped) == 1 and skipped[0][2] == ["alternating", "first_last"]


def test_odoo_side_tag_blocks_a_merge(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "alternating"
    db.commit()
    odoo = _Odoo()
    d = local_day
    rows = []
    for i, h in enumerate((8, 10)):
        rows += [punch(f"v{i}a", "1001", d.replace(hour=h)),
                 punch(f"v{i}b", "1001", d.replace(hour=h, minute=30))]
        cycle(db, tenant, odoo, rows, monkeypatch)
    tenant.pairing_mode = "first_last"
    db.commit()
    _as_first_last(db, odoo)
    next(iter(odoo.attendances.values()))["pairing_mode"] = "alternating"  # Odoo says otherwise
    plan = tool.plan_for(db, tenant, None)
    try:
        tool.apply_plan(db, tenant, odoo, plan[0])
        raise AssertionError("should have been skipped")
    except tool.Skipped:
        pass
    assert len(odoo.attendances) == 2


def test_tool_ignores_accounts_not_on_first_last(db, tenant, capsys):
    tenant.pairing_mode = "alternating"
    db.commit()
    assert tool.run(db, tenant, None, apply=True) == 0
    assert "only applies to those accounts" in capsys.readouterr().out
