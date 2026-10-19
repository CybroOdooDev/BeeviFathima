"""Bring a database to the current schema, whatever state it is in.

Three starting points, one answer:

* **Empty** — ``alembic upgrade head`` builds everything.
* **Already versioned** — ``alembic upgrade head`` applies what is missing.
* **Legacy** — tables exist (made by ``create_all``, and topped up by the old
  ``tools/migrate.py``) but Alembic has never seen the database. It is brought
  level with the models using that same additive tool, checked, and *stamped*
  as being at head, so from then on it is an ordinary versioned database.

Used by ``tools/db_upgrade.py`` (run at every deploy) and by the tests.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text

import app.models  # noqa: F401 — registers every table on Base.metadata
from app.db.base import Base
from app.db.schema_check import detect_drift

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
#: Arbitrary, fixed: two deploys starting at once take turns instead of racing.
ADVISORY_LOCK_KEY = 727274


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_legacy(engine: Engine) -> bool:
    """Has application tables but no Alembic version table."""
    tables = set(inspect(engine).get_table_names())
    return "alembic_version" not in tables and bool(tables & set(Base.metadata.tables))


def pending(engine: Engine) -> bool:
    """Is the database not at the latest revision? (Legacy counts as pending.)"""
    return is_legacy(engine) or current_revision(engine) != head_revision()


def _adopt_legacy(engine: Engine) -> None:
    log.info("Adopting an existing, unversioned database")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "migrate.py"), "--apply"],
        cwd=ROOT, capture_output=True, text=True,
        # The tool reads its database from the environment: aim it at this one.
        env={**os.environ, "DATABASE_URL": engine.url.render_as_string(hide_password=False)},
    )
    if result.returncode != 0:
        raise RuntimeError("Could not bring the existing database level with the models:\n"
                           + result.stdout + result.stderr)
    drift = detect_drift(engine, Base.metadata)
    if not drift.is_empty:
        raise RuntimeError(f"The existing database still differs from the models: {drift.summary()}")


def upgrade(engine: Engine) -> str:
    """Upgrade ``engine``'s database to head and return the revision it is at."""
    config = alembic_config()
    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
            connection.commit()
        try:
            if is_legacy(engine):
                _adopt_legacy(engine)
                config.attributes["connection"] = connection
                command.stamp(config, "head")
            else:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            connection.commit()
        finally:
            if postgres:
                connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
                connection.commit()
    return current_revision(engine) or ""
