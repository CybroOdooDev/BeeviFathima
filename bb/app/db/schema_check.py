"""Compare the models against the live database.

``create_all`` adds missing tables but never alters an existing one, so a model
that gains a column has that column silently absent at runtime. The first query
mentioning it fails with ``no such column`` — from a request, halfway through a
sync, with a stack trace that points at SQLAlchemy rather than at the upgrade
that was never applied.

So the check runs at boot and says what to do. Shared with ``tools/migrate.py``,
which applies it, so the two cannot disagree about what "missing" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, inspect


@dataclass
class SchemaDrift:
    missing_tables: list[str] = field(default_factory=list)
    #: (table, column) pairs the models declare and the database does not have.
    missing_columns: list[tuple[str, str]] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)
    #: (table, column) the database still marks NOT NULL while the model allows
    #: null. Reported separately because relaxing one is not an additive change:
    #: PostgreSQL takes an ALTER, SQLite has to rebuild the table.
    over_strict_columns: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.missing_tables
            or self.missing_columns
            or self.missing_indexes
            or self.over_strict_columns
        )

    def summary(self) -> str:
        parts = []
        if self.missing_tables:
            parts.append(f"{len(self.missing_tables)} table(s): "
                         + ", ".join(sorted(self.missing_tables)))
        if self.missing_columns:
            parts.append(f"{len(self.missing_columns)} column(s): "
                         + ", ".join(f"{t}.{c}" for t, c in sorted(self.missing_columns)))
        if self.missing_indexes:
            parts.append(f"{len(self.missing_indexes)} index(es)")
        if self.over_strict_columns:
            parts.append(
                f"{len(self.over_strict_columns)} column(s) still NOT NULL: "
                + ", ".join(f"{t}.{c}" for t, c in sorted(self.over_strict_columns))
            )
        return "; ".join(parts) or "none"


def detect_drift(engine: Engine, metadata) -> SchemaDrift:
    """What the models declare that the database does not have.

    One direction only. A column the database has and the models do not is left
    alone and not reported: that is what a rollback looks like, and dropping it
    would be the destructive half of a migration this system does not do.
    """
    drift = SchemaDrift()
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())

    for name, table in metadata.tables.items():
        if name not in live_tables:
            drift.missing_tables.append(name)
            continue

        live_columns = {c["name"]: c for c in inspector.get_columns(name)}
        for column in table.columns:
            live = live_columns.get(column.name)
            if live is None:
                drift.missing_columns.append((name, column.name))
                continue
            # The model now allows null and the database does not. An insert
            # that relies on the new freedom fails with a constraint error that
            # names the column but not the reason.
            if column.nullable and not live.get("nullable", True):
                drift.over_strict_columns.append((name, column.name))

        live_indexes = {i["name"] for i in inspector.get_indexes(name)}
        drift.missing_indexes += [
            i.name for i in table.indexes if i.name not in live_indexes
        ]

    return drift
