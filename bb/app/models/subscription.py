"""Subscription plans: the tiers a tenant's account is sold under.

A plan is a bundle of enforced limits, not a billing record. Charging is
Stripe's job when online billing is on (app.services.billing), linked to a
plan only by ``stripe_price_id``; without it the platform's own choice
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
    #: Hidden from *new* assignment instead (Platform → Plans → Retired).
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Offered to a self-signup or a staff-created account when neither picks
    #: one. Exactly zero or one plan should carry this — nothing enforces
    #: that at the database level, so tools/seed_plans.py clears it from
    #: every other plan whenever it sets one.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Display price. What a customer is actually charged is the Stripe
    #: Price below (when online billing is on) — keep the two in step.
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

    #: The most biometric terminals this account can use. Counted over every
    #: terminal it has ever added (app.services.device_limits); the oldest ones
    #: fill the allowance. A terminal beyond it is still recorded and its
    #: punches kept, but they are *held* — not sent to Odoo — until the plan
    #: allows it, so nothing is lost and an upgrade releases them.
    max_devices: Mapped[int | None] = mapped_column(Integer)

    #: The Stripe Price (``price_…``) a customer is charged for this plan,
    #: monthly. Null means the plan cannot be bought online — Checkout
    #: refuses it and staff assign it by hand, as before billing existed.
    #: Set in the console (Platform → Plans), or by tools/seed_plans.py from
    #: STRIPE_PRICE_<PLAN NAME> env vars.
    stripe_price_id: Mapped[str | None] = mapped_column(String(80), index=True)

    #: What a customer who pays once a year is charged, in cents, and the
    #: Stripe Price (recurring, yearly) it is charged through. Both optional:
    #: a plan with no yearly Stripe Price is monthly-only online.
    yearly_price_cents: Mapped[int | None] = mapped_column(Integer)
    stripe_yearly_price_id: Mapped[str | None] = mapped_column(String(80), index=True)

    def price_id_for(self, interval: str | None) -> str | None:
        """The Stripe Price to charge for ``interval`` ("month" or "year")."""
        return self.stripe_yearly_price_id if interval == "year" else self.stripe_price_id
