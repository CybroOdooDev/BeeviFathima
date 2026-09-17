"""Declarative base and the two mixins every model uses."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    """32 hex chars, no dashes — hence String(32) on every id and mirror column."""
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class UUIDPk:
    """Ids are generated in Python at flush, not by the database.

    That keeps the id available before the INSERT, which the sync engine relies
    on when it builds relationships in memory before committing.
    """

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class Timestamped:
    """Timestamps come from the database clock, so they agree across processes."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
