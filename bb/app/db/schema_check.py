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

from sqlalchemy import Engine, UniqueConstraint, inspect


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
    #: (table, constraint name) a named unique constraint whose live column set
    #: no longer matches what the model declares for that same name — e.g. a
    #: constraint widened from (tenant_id, serial_number) to (tenant_id,
    #: source_id, serial_number). Same "not additive" reason as above: neither
    #: backend can ALTER a constraint's columns in place, so this needs the
    #: same table rebuild.
    mismatched_unique_constraints: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.missing_tables
            or self.missing_columns
            or self.missing_indexes
            or self.over_strict_columns
            or self.mismatched_unique_constraints
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
        if self.mismatched_unique_constraints:
            parts.append(
                f"{len(self.mismatched_unique_constraints)} constraint(s) changed shape: "
                + ", ".join(f"{t}.{c}" for t, c in sorted(self.mismatched_unique_constraints))
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

        # Matched by name, not by column set: a constraint the model dropped
        # entirely is a rollback (left alone, same rule as a dropped column),
        # and a brand new constraint name is caught by no live match at all —
        # only a *same-named* constraint whose columns disagree is drift here.
        live_uniques = {
            u["name"]: set(u["column_names"])
            for u in inspector.get_unique_constraints(name)
            if u["name"]
        }
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint) or not constraint.name:
                continue
            live_cols = live_uniques.get(constraint.name)
            if live_cols is None:
                continue
            model_cols = {c.name for c in constraint.columns}
            if live_cols != model_cols:
                drift.mismatched_unique_constraints.append((name, constraint.name))

    return drift
