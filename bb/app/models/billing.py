"""Stripe webhook bookkeeping."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped, UUIDPk


class StripeEvent(Base):
    """One row per Stripe event already handled.

    Stripe delivers at least once, and retries an event until it gets a 2xx —
    so the same ``evt_…`` can arrive twice, or long after a later one. The
    handler inserts the id first and skips an event it has seen; the state it
    applies is always re-read from Stripe (see app.services.billing), so the
    order events arrive in does not matter either.
    """

    __tablename__ = "stripe_event"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PendingSignup(Base, UUIDPk, Timestamped):
    """A website registration waiting on payment.

    The "buy a plan" path creates no account until Stripe confirms the
    payment: this row carries what the visitor typed through Checkout (its id
    rides along as Checkout metadata) and the webhook turns it into a tenant
    and an owner (app.services.onboarding.complete_paid_signup). An abandoned
    or failed checkout leaves only this row — no half-made account, no email.
    """

    __tablename__ = "pending_signup"

    company_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    plan_id: Mapped[str] = mapped_column(String(32), nullable=False)
    billing_interval: Mapped[str] = mapped_column(String(5), default="month", server_default="month")
    #: Set when the webhook made the account, so a replayed event is a no-op.
    tenant_id: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
