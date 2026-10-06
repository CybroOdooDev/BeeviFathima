"""Enquiries and demo requests from the marketing website."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped, UUIDPk

#: The sales pipeline, in order. A lead is in exactly one stage; "won" and
#: "lost" are the two ways out. Moving between stages is free in either
#: direction (a lead that goes quiet can be moved back) — every move is
#: recorded in ContactEvent, which is what makes the history trustworthy.
PIPELINE_STAGES = ("new", "contacted", "demo", "qualified", "won", "lost")
OPEN_STAGES = ("new", "contacted", "demo", "qualified")
CONTACT_STATUSES = PIPELINE_STAGES


class ContactRequest(Base, UUIDPk, Timestamped):
    """One submission of the website's Contact / Book a demo form.

    Kept in the database (not only emailed) so a lead cannot be lost to a full
    inbox or a failed send, and so staff can see what has been answered.
    """

    __tablename__ = "contact_request"
    __table_args__ = (Index("ix_contact_status_created", "status", "created_at"),)

    #: "Demo" or "Sales question".
    topic: Mapped[str] = mapped_column(String(40), default="Demo", nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    company: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    employees: Mapped[str | None] = mapped_column(String(40))
    odoo_version: Mapped[str | None] = mapped_column(String(40))
    odoo_hosting: Mapped[str | None] = mapped_column(String(40))
    biometric_system: Mapped[str | None] = mapped_column(String(120))
    device_setup: Mapped[str | None] = mapped_column(String(60))
    message: Mapped[str | None] = mapped_column(Text)
    #: Demo requests: when the visitor would like it, in their own timezone.
    preferred_date: Mapped[str | None] = mapped_column(String(10))      # YYYY-MM-DD
    preferred_window: Mapped[str | None] = mapped_column(String(20))    # Morning / Afternoon / Evening
    timezone: Mapped[str | None] = mapped_column(String(64))
    #: Where the request came from, for spotting abuse. Never shown to the visitor.
    ip: Mapped[str | None] = mapped_column(String(64))

    #: The pipeline stage (see PIPELINE_STAGES). Named ``status`` for history.
    status: Mapped[str] = mapped_column(String(20), default="new", server_default="new", nullable=False)
    #: When the lead last changed stage — how long it has sat where it is.
    stage_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why a lead was lost ("too expensive", "chose a competitor"), when known.
    lost_reason: Mapped[str | None] = mapped_column(String(160))
    #: Staff's own notes — "called Tuesday, wants Hikvision support".
    notes: Mapped[str | None] = mapped_column(Text)
    handled_by: Mapped[str | None] = mapped_column(String(254))


class ContactEvent(Base, UUIDPk, Timestamped):
    """One entry in a lead's history: a stage move or a note."""

    __tablename__ = "contact_event"
    __table_args__ = (Index("ix_contact_event_lead_created", "contact_id", "created_at"),)

    contact_id: Mapped[str] = mapped_column(
        ForeignKey("contact_request.id", ondelete="CASCADE"), nullable=False
    )
    #: "stage" (moved between stages) or "note".
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    from_stage: Mapped[str | None] = mapped_column(String(20))
    to_stage: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(254))
