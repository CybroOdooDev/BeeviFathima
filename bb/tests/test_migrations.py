"""Alembic: the migrations and the models must always agree."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401
from app.db import migrations
from app.db.base import Base
from app.db.schema_check import detect_drift


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # env.py reads the URL from settings; point it at this test's own file.
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setattr("app.core.config.settings.database_url", url)
    eng = create_engine(url)
    yield eng
    eng.dispose()


def _diff(engine):
    with engine.connect() as connection:
        return compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata)


def test_an_empty_database_is_built_to_exactly_the_models(engine):
    assert migrations.upgrade(engine) == migrations.head_revision()
    assert detect_drift(engine, Base.metadata).is_empty
    # Nothing a new revision would need to add: this is what fails when someone
    # edits a model and forgets `alembic revision --autogenerate`.
    assert _diff(engine) == [], _diff(engine)


def test_running_it_again_changes_nothing(engine):
    migrations.upgrade(engine)
    assert migrations.upgrade(engine) == migrations.head_revision()
    assert not migrations.pending(engine)


def test_a_database_from_before_alembic_is_brought_level_and_adopted(engine):
    Base.metadata.create_all(engine)
    with engine.begin() as c:                      # a legacy install that missed a release
        c.execute(text("ALTER TABLE tenant DROP COLUMN alert_state"))
    assert migrations.is_legacy(engine) and migrations.pending(engine)
    assert migrations.upgrade(engine) == migrations.head_revision()
    assert not migrations.is_legacy(engine)
    assert "alert_state" in {c["name"] for c in inspect(engine).get_columns("tenant")}
    assert detect_drift(engine, Base.metadata).is_empty


def test_there_is_exactly_one_head():
    from alembic.script import ScriptDirectory

    assert len(ScriptDirectory.from_config(migrations.alembic_config()).get_heads()) == 1
