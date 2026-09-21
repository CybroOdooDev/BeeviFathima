"""Subscription plans: the tiers a tenant's account is sold under.

A plan is a bundle of enforced limits, not a billing record. Nothing here
charges a card or talks to a payment provider — the platform's own choice
(see ``app.services.scheduling.sweep_subscriptions``) is to drive activation
off an internal "paid through" date on the tenant, and let staff move that
date by whatever process they already use to get paid. A plan just says what
an account on it is allowed to do, so a cheaper tier can be a real constraint
and not just a label on an invoice.

Limits are nullable and null means unlimited, so a plan can cap one thing and
leave the other alone, and the built-in "no plan assigned" state — every
tenant that existed before this feature, and any account staff choose to
leave unassigned — enforces nothing at all. That is the safe default for a
rollout that must not suddenly restrict an existing customer.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped, UUIDPk


class SubscriptionPlan(Base, UUIDPk, Timestamped):
    __tablename__ = "subscription_plan"

    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)

    #: A one-line description of what the tier is for, shown wherever a plan
    #: is picked. Not enforced by anything — purely so a staff member
    #: assigning a plan can tell them apart without memorising every cap.
    description: Mapped[str | None] = mapped_column(String(200))

    #: Retired plans are kept, never deleted — a tenant already on one must
    #: keep working, and this row is the only record of what it promised.
    #: Hidden from *new* assignment instead; see tools/seed_plans.py.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Offered to a self-signup or a staff-created account when neither picks
    #: one. Exactly zero or one plan should carry this — nothing enforces
    #: that at the database level, so tools/seed_plans.py clears it from
    #: every other plan whenever it sets one.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Informational only. Nothing in this codebase processes a payment —
    #: shown to staff so the console does not need a separate price list
    #: sitting next to the plan names.
    monthly_price_cents: Mapped[int | None] = mapped_column(Integer)

    #: Enforced in ``app.services.sync_engine._resolve_mappings``: once a
    #: tenant has this many badges mapped to an Odoo employee, matching stops
    #: creating new ones. Existing mappings are never removed by a downgrade
    #: — only new badges are held back — so lowering a tenant's cap cannot
    #: break attendance for people already relying on it.
    max_employees: Mapped[int | None] = mapped_column(Integer)

    #: Enforced on the customer's own settings save (``PATCH /api/v1/tenant``
    #: — see ``app.api.v1.sync.update_tenant``): the fastest interval this
    #: plan allows them to choose for themselves. Staff can still set a
    #: tighter one from the console — this limits self-service, not the
    #: platform's own ability to make an exception.
    min_sync_interval_minutes: Mapped[int | None] = mapped_column(Integer)
