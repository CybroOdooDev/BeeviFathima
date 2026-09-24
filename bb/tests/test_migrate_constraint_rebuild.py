"""tools/migrate.py's newest capability: reshaping a named unique constraint.

Companion to the Device.uq_device_serial widening (tenant_id, serial_number)
-> (tenant_id, source_id, serial_number) — see
tests/test_device_discovery.py::test_two_sources_with_the_same_serial_number_do_not_collide
for the application-level bug that change fixes. This file tests the
migration machinery itself: given a database that still has the *old* shape
of a named constraint, does detect_drift notice, and does --apply actually
fix it (on SQLite, via the same whole-table rebuild relax_not_null already
used for NOT NULL changes) without losing any rows.

A synthetic device table is built here with a deliberately old-shaped
uq_device_serial, rather than depending on some future migration this repo
may or may not still carry — that keeps this test meaningful even after the
Device model itself has moved on to its next change.
"""
from __future__ import annotations

import importlib
import re
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.db.base import Base
from app.db.schema_check import detect_drift
from app.models import DeviceSource, Tenant
import app.models  # noqa: F401 — registers every table on Base.metadata


def _old_shape_device_ddl(engine, drop_columns: tuple[str, ...] = ()) -> str:
    """DDL for the real, current ``device`` table, but with the
    pre-widening unique constraint — simulating a database that predates
    this migration, whatever the model's constraint columns actually are
    today. ``drop_columns`` additionally strips named column definitions
    from the DDL, to simulate a database that also predates a column this
    same migration adds — the real shape a customer's database was in when
    both landed in one release (see
    test_apply_adds_a_column_and_reshapes_a_constraint_together_on_one_table
    below, the regression for the exact crash that combination caused).

    Built by compiling the model's own current CreateTable DDL and then
    editing the text, rather than mutating a cloned Table object — a
    Table's column collection is read-only once built, so removing a
    column from it isn't something the API supports; string surgery on
    generated DDL that this test immediately throws away is a fair trade
    for not fighting that.
    """
    live = Base.metadata.tables["device"]
    ddl = str(CreateTable(live).compile(engine))
    ddl = re.sub(
        r"CONSTRAINT uq_device_serial UNIQUE \([^)]*\)",
        "CONSTRAINT uq_device_serial UNIQUE (tenant_id, serial_number)",
        ddl,
    )
    for name in drop_columns:
        ddl = re.sub(rf"\n\s*{re.escape(name)} [^\n]*,", "", ddl)
    return ddl


def _build_migrate_env(monkeypatch, drop_columns: tuple[str, ...] = ()):
    """A real SQLite file, current-shaped everywhere except ``device``
    (deliberately old-shaped there), with tools.migrate pointed at it."""
    tmp_dir = tempfile.mkdtemp()
    db_path = Path(tmp_dir) / "migrate_test.db"
    database_url = f"sqlite:///{db_path}"
    test_engine = create_engine(database_url, connect_args={"check_same_thread": False})

    Base.metadata.create_all(test_engine)
    with test_engine.begin() as connection:
        connection.execute(text("DROP TABLE device"))
        connection.execute(text(_old_shape_device_ddl(test_engine, drop_columns)))

    # tools.migrate reads `engine` and `settings.database_url` as module
    # globals rather than parameters, so the test points those at the
    # scratch database rather than the real one for the fixture's lifetime.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    migrate = importlib.import_module("tools.migrate")
    monkeypatch.setattr(migrate, "engine", test_engine)
    monkeypatch.setattr(migrate.settings, "database_url", database_url)

    return migrate, test_engine


@pytest.fixture
def migrate_env(monkeypatch):
    migrate, test_engine = _build_migrate_env(monkeypatch)
    yield migrate, test_engine
    test_engine.dispose()


@pytest.fixture
def migrate_env_missing_column(monkeypatch):
    """Same as ``migrate_env``, but the old-shaped ``device`` table also
    lacks ``missing_since`` — the real state a customer's database was in
    when this same release both added that column and widened
    uq_device_serial: the exact combination that crashed ``--apply`` with
    "no such column: missing_since" the first time this shipped (rebuild_table
    was copying the model's full column list from a live table that didn't
    have the new column yet)."""
    migrate, test_engine = _build_migrate_env(monkeypatch, drop_columns=("missing_since",))
    yield migrate, test_engine
    test_engine.dispose()


def _make_tenant_and_sources(engine) -> tuple[str, str, str]:
    """Real Tenant/DeviceSource rows (both tables already current-shaped by
    create_all) so the Device rows inserted for the test satisfy real
    foreign keys rather than relying on SQLite's FK checking being off."""
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        tenant = Tenant(name="Acme", slug="acme")
        session.add(tenant)
        session.flush()
        source_a = DeviceSource(
            tenant_id=tenant.id, name="Company1 Biotime", base_url="https://a.test", username="u"
        )
        source_b = DeviceSource(
            tenant_id=tenant.id, name="Company2 Biotime", base_url="https://b.test", username="u"
        )
        session.add_all([source_a, source_b])
        session.commit()
        return tenant.id, source_a.id, source_b.id


def _insert_device(engine, *, tenant_id, source_id, serial_number, device_id):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO device (id, tenant_id, source_id, serial_number, "
                "is_enabled, punch_count, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :source_id, :serial_number, 1, 0, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {
                "id": device_id,
                "tenant_id": tenant_id,
                "source_id": source_id,
                "serial_number": serial_number,
            },
        )


def test_detect_drift_reports_the_old_shaped_constraint(migrate_env):
    _migrate, test_engine = migrate_env
    drift = detect_drift(test_engine, Base.metadata)
    assert ("device", "uq_device_serial") in drift.mismatched_unique_constraints
    assert not drift.is_empty
    assert "uq_device_serial" in drift.summary()


def test_apply_reshapes_the_constraint_and_keeps_the_rows(migrate_env, capsys):
    migrate, test_engine = migrate_env
    tenant_id, source_a, source_b = _make_tenant_and_sources(test_engine)

    # One row under the old (tenant_id, serial_number) constraint — this is
    # exactly the row a real customer would have on disk going into the fix.
    _insert_device(
        test_engine,
        tenant_id=tenant_id,
        source_id=source_a,
        serial_number="GATE-01",
        device_id="device-1",
    )

    drift_before = detect_drift(test_engine, Base.metadata)
    assert ("device", "uq_device_serial") in drift_before.mismatched_unique_constraints

    # main() parses sys.argv itself; swap it for the duration of the call
    # rather than fighting argv in-process.
    sys_argv_backup = sys.argv
    sys.argv = ["migrate.py", "--apply"]
    try:
        exit_code = migrate.main()
    finally:
        sys.argv = sys_argv_backup
    assert exit_code == 0

    drift_after = detect_drift(test_engine, Base.metadata)
    assert drift_after.mismatched_unique_constraints == []

    inspector = inspect(test_engine)
    live = {u["name"]: set(u["column_names"]) for u in inspector.get_unique_constraints("device")}
    model_constraint = next(
        constraint
        for constraint in Base.metadata.tables["device"].constraints
        if isinstance(constraint, UniqueConstraint) and constraint.name == "uq_device_serial"
    )
    model_cols = {column.name for column in model_constraint.columns}
    assert live["uq_device_serial"] == model_cols

    # The pre-existing row survived the rebuild.
    with test_engine.connect() as connection:
        rows = connection.execute(text("SELECT id, tenant_id, source_id, serial_number FROM device")).all()
    assert len(rows) == 1
    assert rows[0][0] == "device-1"
    assert rows[0][3] == "GATE-01"

    # And the whole point of the widened constraint: a second source can
    # now register the same serial number for the same tenant, which the
    # old (tenant_id, serial_number) constraint would have rejected.
    _insert_device(
        test_engine,
        tenant_id=tenant_id,
        source_id=source_b,
        serial_number="GATE-01",
        device_id="device-2",
    )
    with test_engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM device")).scalar()
    assert count == 2

    captured = capsys.readouterr()
    assert "reshaped unique constraint device.uq_device_serial" in captured.out


def test_apply_backs_up_the_sqlite_file_before_reshaping(migrate_env):
    migrate, test_engine = migrate_env
    db_path = Path(migrate.settings.database_url.removeprefix("sqlite:///"))

    sys_argv_backup = sys.argv
    sys.argv = ["migrate.py", "--apply"]
    try:
        migrate.main()
    finally:
        sys.argv = sys_argv_backup

    backups = list(db_path.parent.glob(f"{db_path.stem}.backup-*"))
    assert backups, "expected a .backup-<timestamp> copy before the rebuild"


def test_apply_adds_a_column_and_reshapes_a_constraint_together_on_one_table(
    migrate_env_missing_column,
):
    """The exact crash a customer hit running --apply: a live database whose
    ``device`` table needed both a brand new column (``missing_since``) and
    a reshaped unique constraint at once. rebuild_table used to compile its
    INSERT...SELECT from the model's full column list regardless of what the
    live table actually had yet, so it tried to SELECT a column that would
    only exist after the (separate, later-in-main()) ADD COLUMN step —
    "no such column: missing_since". It must now succeed in one --apply run,
    whichever order those two fixes happen to run in."""
    migrate, test_engine = migrate_env_missing_column
    tenant_id, source_a, _source_b = _make_tenant_and_sources(test_engine)
    _insert_device(
        test_engine,
        tenant_id=tenant_id,
        source_id=source_a,
        serial_number="GATE-01",
        device_id="device-1",
    )

    drift_before = detect_drift(test_engine, Base.metadata)
    assert ("device", "missing_since") in drift_before.missing_columns
    assert ("device", "uq_device_serial") in drift_before.mismatched_unique_constraints

    sys_argv_backup = sys.argv
    sys.argv = ["migrate.py", "--apply"]
    try:
        exit_code = migrate.main()
    finally:
        sys.argv = sys_argv_backup
    assert exit_code == 0

    drift_after = detect_drift(test_engine, Base.metadata)
    assert drift_after.is_empty

    # The pre-existing row survived, and the new column reads back as NULL
    # rather than erroring — there was nothing to backfill it from.
    with test_engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, serial_number, missing_since FROM device")
        ).one()
    assert row[0] == "device-1"
    assert row[1] == "GATE-01"
    assert row[2] is None

    # And the constraint is genuinely widened, not just column-added.
    inspector = inspect(test_engine)
    live = {u["name"]: set(u["column_names"]) for u in inspector.get_unique_constraints("device")}
    assert live["uq_device_serial"] == {"tenant_id", "source_id", "serial_number"}
