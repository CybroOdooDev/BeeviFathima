#!/usr/bin/env python3
"""Add columns and tables the models have gained since the database was created.

``create_all`` adds missing *tables* but never touches an existing one, so a new
column on a model that already has a table is simply absent at runtime — and the
first query that mentions it fails with "no such column". This closes that gap
for the only change that is safe to make automatically: **adding** a nullable
column, or one with a default.

    python3 tools/migrate.py            # report what is missing
    python3 tools/migrate.py --apply    # add it

Deliberately limited. It will not rename, drop, retype or backfill anything, and
it refuses a column it cannot add without inventing data. Those need Alembic and
a decision about the existing rows — this is here so a routine additive change
does not require either.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import MetaData, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.schema import CreateColumn, CreateTable  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.schema_check import detect_drift  # noqa: E402
from app.db.session import engine  # noqa: E402
import app.models  # noqa: E402,F401 — registers every table on Base.metadata


def plan() -> tuple[list[str], list[tuple[str, str]], list, list[str]]:
    """Return (missing tables, addable columns, missing indexes, blocked).

    The detection comes from ``app.db.schema_check``, which the app also calls at
    boot to warn about exactly this — one definition of "missing", so the warning
    and the fix cannot disagree. Deciding what is *safe to add* is this tool's
    own job and stays here.
    """
    drift = detect_drift(engine, Base.metadata)

    addable: list[tuple[str, str]] = []
    blocked: list[str] = []

    for table_name, column_name in drift.missing_columns:
        column = Base.metadata.tables[table_name].columns[column_name]
        # A NOT NULL column cannot be added to a table that already has rows
        # unless the DDL carries a DEFAULT — there is no value to put in them.
        #
        # ``column.default`` does NOT count. That is the Python-side default,
        # applied by the ORM on insert and never rendered into DDL, so a model
        # declaring `default=False` still emits a bare `BOOLEAN NOT NULL` and
        # the ALTER fails. Only ``server_default`` reaches the database. Treating
        # the two as equivalent is how a migration passes review and then dies
        # on the first populated table it meets.
        if not column.nullable and column.server_default is None:
            hint = (
                " (it has a Python-side default, which is not rendered into DDL —"
                " add server_default to the model)"
                if column.default is not None
                else ""
            )
            blocked.append(
                f"{table_name}.{column_name} is NOT NULL with no server default{hint}"
            )
            continue
        addable.append((table_name, str(CreateColumn(column).compile(engine))))

    # ALTER TABLE ADD COLUMN does not bring the column's index with it, so a
    # migrated table ends up correct but slow — and slow only once the ledger is
    # big enough for anyone to notice.
    by_name = {
        index.name: index
        for table in Base.metadata.tables.values()
        for index in table.indexes
    }
    missing_indexes = [by_name[n] for n in drift.missing_indexes if n in by_name]

    return drift.missing_tables, addable, missing_indexes, blocked


def backup_sqlite() -> str | None:
    """Copy the database file before a rebuild. Returns where it went.

    Only relaxing a NOT NULL needs this, and only on SQLite, where the
    documented way to do it is to build a new table, copy the rows across and
    swap the names. Everything else this tool does is additive and cannot lose
    a row; that one can, so it does not run without a copy sitting beside it.
    """
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    source = pathlib.Path(url.database)
    if not source.exists():
        return None
    target = source.with_name(
        f"{source.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{source.suffix}"
    )
    shutil.copy2(source, target)
    return str(target)


def relax_not_null(table_name: str, column_name: str) -> None:
    """Let a column that the model now calls nullable actually hold null.

    PostgreSQL has a statement for it. SQLite does not — ``ALTER COLUMN`` does
    not exist there — so the table is rebuilt: create the new shape under a
    temporary name, copy every row, drop the old one, rename. That is SQLite's
    own documented procedure, and it runs inside a transaction with foreign keys
    off so a half-done rebuild rolls back whole.
    """
    backend = make_url(settings.database_url).get_backend_name()

    if backend != "sqlite":
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE {table_name} ALTER COLUMN {column_name} DROP NOT NULL")
            )
        return

    table = Base.metadata.tables[table_name]
    column_list = ", ".join(c.name for c in table.columns)
    temp = f"{table_name}__rebuild"

    # The new shape, compiled to SQL under a temporary name.
    #
    # The referenced tables are copied into the staging metadata first: a
    # foreign key copied on its own has nothing to resolve against, and
    # compiling it raises rather than producing SQL. Only the new table is ever
    # compiled, so the copies exist purely to satisfy that lookup.
    #
    # Indexes are left off — the rename would not carry their names across
    # anyway, so they are rebuilt from the model afterwards.
    staging = MetaData()
    for constraint in table.foreign_key_constraints:
        referred = constraint.referred_table
        if referred.name not in staging.tables and referred.name != table_name:
            referred.to_metadata(staging)
    new_table = table.to_metadata(staging, name=temp)
    new_table.indexes.clear()
    create_sql = str(CreateTable(new_table).compile(engine))

    # Driven through the DBAPI connection rather than a SQLAlchemy one: the
    # foreign-key PRAGMA cannot take effect inside a transaction, and
    # SQLAlchemy opens one the moment anything is executed.
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN")
        try:
            cursor.execute(create_sql)
            cursor.execute(
                f"INSERT INTO {temp} ({column_list}) "  # noqa: S608 — names from the model
                f"SELECT {column_list} FROM {table_name}"
            )
            cursor.execute(f"DROP TABLE {table_name}")
            cursor.execute(f"ALTER TABLE {temp} RENAME TO {table_name}")
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise
        finally:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    finally:
        raw.close()

    # The rename does not carry the model's index names across, so rebuild them.
    for index in table.indexes:
        try:
            index.create(bind=engine, checkfirst=True)
        except Exception as exc:  # noqa: BLE001 — an existing index is fine
            log_line = f"  (index {index.name}: {exc})"
            print(log_line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually make the changes.")
    args = parser.parse_args()

    print(f"database: {settings.database_url}\n")
    missing_tables, addable, missing_indexes, blocked = plan()
    over_strict = detect_drift(engine, Base.metadata).over_strict_columns

    if not (missing_tables or addable or missing_indexes or blocked or over_strict):
        print("Schema is up to date — nothing to add.")
        return 0

    for table in missing_tables:
        print(f"  MISSING TABLE   {table}")
    for table, ddl in addable:
        print(f"  ADD COLUMN      {table}.{ddl.split()[0]}   ({ddl})")
    for index in missing_indexes:
        print(f"  ADD INDEX       {index.name}")
    for table, column in over_strict:
        print(f"  DROP NOT NULL   {table}.{column}"
              f"   (the model allows null; the database does not)")
    for note in blocked:
        print(f"  NEEDS ATTENTION {note}")

    if over_strict:
        print("\nDropping a NOT NULL is the one change here that is not additive:"
              "\non SQLite the table is rebuilt, so the database file is copied"
              "\nfirst and the rebuild runs in a transaction.")

    if not args.apply:
        print("\nReport only. Re-run with --apply to make the changes.")
        return 0

    if over_strict:
        backup = backup_sqlite()
        if backup:
            print(f"\nbacked up to {backup}")
        for table, column in over_strict:
            relax_not_null(table, column)
            print(f"dropped NOT NULL from {table}.{column}")

    if missing_tables:
        Base.metadata.create_all(engine)
        print(f"\ncreated {len(missing_tables)} table(s)")

    if addable:
        with engine.begin() as connection:
            for table, ddl in addable:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
                print(f"added {table}.{ddl.split()[0]}")

    for index in missing_indexes:
        # checkfirst, because create_all above may already have built the index
        # along with a table it created in the same run.
        index.create(bind=engine, checkfirst=True)
        print(f"added index {index.name}")

    if blocked:
        print(f"\n{len(blocked)} change(s) left alone — see above.")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
