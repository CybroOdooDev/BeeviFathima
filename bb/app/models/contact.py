"""Enquiries and demo requests from the marketing website."""

from __future__ import annotations

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped, UUIDPk

CONTACT_STATUSES = ("new", "contacted", "demo_booked", "won", "closed")


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

    status: Mapped[str] = mapped_column(String(20), default="new", server_default="new", nullable=False)
    #: Staff's own notes — "called Tuesday, wants Hikvision support".
    notes: Mapped[str | None] = mapped_column(Text)
    handled_by: Mapped[str | None] = mapped_column(String(254))
