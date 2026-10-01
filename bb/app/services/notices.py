"""Emails telling an account that its syncing has stopped, or will.

Sent when BioBridge itself stops an account — a renewal date passing without
payment, Stripe reporting a failed renewal, or a cancellation taking effect —
so the customer hears it from us instead of noticing attendance went quiet.
Best-effort: a mail failure is logged and never undoes the status change.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Tenant, User, UserRole
from app.services.mailer import send_email

log = logging.getLogger(__name__)

WHY = {
    "trial_ended": ("Your BioBridge trial has ended",
                    "Your free trial is over and no plan was chosen, so BioBridge has paused syncing "
                    "attendance from your devices into Odoo. Choose a plan to switch it back on."),
    "lapsed": ("Your BioBridge subscription has lapsed",
               "Your plan's renewal date has passed without a renewal, so BioBridge has paused syncing "
               "attendance from your devices into Odoo."),
    "payment_failed": ("Payment failed — BioBridge syncing is paused",
                       "Stripe couldn't charge your card for this month's renewal, so BioBridge has paused "
                       "syncing attendance into Odoo. Stripe will retry the card; you can also pay the open "
                       "invoice now under Settings → Billing."),
    "cancelled": ("Your BioBridge subscription has ended",
                  "Your subscription was cancelled and the paid period is over, so BioBridge has stopped "
                  "syncing attendance into Odoo."),
}


def notify_sync_stopped(db: Session, tenant: Tenant, why: str) -> None:
    subject, lead = WHY[why]
    from app.services.onboarding import login_url

    recipients = db.scalars(select(User.email).where(
        User.tenant_id == tenant.id,
        User.role.in_([UserRole.owner.value, UserRole.admin.value]))).all()
    body = (
        f"Hello,\n\n{lead}\n\n"
        "Nothing has been deleted: your connections, employees and attendance history are all kept, "
        "and syncing resumes — catching up on punches held on the devices — as soon as the "
        "subscription is active again.\n\n"
        f"Renew or choose a plan: {login_url()}  (Settings → Plan / Billing)\n\n"
        "— BioBridge\n"
    )
    for email in recipients:
        try:
            send_email(email, f"{subject} ({tenant.name})", body, db=db)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not send the sync-stopped notice to %s: %s", email, exc)
