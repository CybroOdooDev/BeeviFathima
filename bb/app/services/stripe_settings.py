"""Console → Payments (Stripe): the Stripe keys, saved from the staff console.

Same shape as the Email server settings (app.services.mail_settings): a row
in ``platform_setting`` (key ``stripe``) with the secrets encrypted, taking
precedence over ``STRIPE_SECRET_KEY`` / ``STRIPE_WEBHOOK_SECRET`` from .env
while it is saved and switched on. Price ids stay on the plans themselves
(Console → Plans), so this page only links to them and checks them.

The keys are loaded into app.services.billing at the start of every request
that touches billing (``billing.use_config``), so a save takes effect on the
next request on every process — no restart.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import CryptoError, decrypt, encrypt
from app.models import PlatformSetting, StripeEvent, SubscriptionPlan

log = logging.getLogger(__name__)

KEY = "stripe"
CRYPTO_KEY = "platform:stripe"

#: What the webhook endpoint in Stripe must be subscribed to.
WEBHOOK_EVENTS = (
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
)


def _row(db: Session) -> PlatformSetting | None:
    return db.get(PlatformSetting, KEY)


def _stored(db: Session) -> dict:
    row = _row(db)
    if row is None:
        return {}
    try:
        return json.loads(row.value or "{}")
    except ValueError:
        log.error("platform_setting 'stripe' is not valid JSON — ignoring it")
        return {}


def _secret(data: dict, field: str) -> str:
    try:
        return decrypt(data.get(field), CRYPTO_KEY) or ""
    except CryptoError:
        log.error("The saved Stripe %s cannot be decrypted (MASTER_ENCRYPTION_KEY changed?)", field)
        return ""


def console_keys(db: Session) -> tuple[str, str] | None:
    """(secret key, webhook secret) saved in the console and switched on — or
    None, meaning "use .env"."""
    data = _stored(db)
    if not data or not data.get("enabled", True):
        return None
    secret = _secret(data, "secret_key_enc")
    if not secret:
        return None
    return secret, _secret(data, "webhook_secret_enc")


def save(db: Session, data: dict, actor_email: str | None) -> None:
    merged = dict(_stored(db))
    if data.get("enabled") is not None:
        merged["enabled"] = bool(data["enabled"])
    for field, enc in (("secret_key", "secret_key_enc"), ("webhook_secret", "webhook_secret_enc")):
        if data.get(f"clear_{field}"):
            merged.pop(enc, None)
        elif data.get(field):
            merged[enc] = encrypt(data[field].strip(), CRYPTO_KEY)
    row = _row(db)
    if row is None:
        row = PlatformSetting(key=KEY)
        db.add(row)
    row.value = json.dumps(merged)
    row.updated_by = actor_email
    db.commit()


def _hint(key: str) -> str | None:
    if not key:
        return None
    prefix = key.split("_")[0] + "_" + (key.split("_")[1] if key.count("_") >= 2 else "")
    return f"{prefix}_…{key[-4:]}"


def mode_of(key: str) -> str | None:
    if "_test_" in key:
        return "test"
    if "_live_" in key:
        return "live"
    return None


def public_view(db: Session) -> dict:
    """Everything the console shows — the keys only as a hint (sk_test_…4242)."""
    data = _stored(db)
    saved_secret = _secret(data, "secret_key_enc") if data else ""
    saved_webhook = _secret(data, "webhook_secret_enc") if data else ""
    console = console_keys(db)
    if console:
        active_source, active_key = "database", console[0]
        active_webhook = bool(console[1])
    elif settings.stripe_secret_key:
        active_source, active_key = "environment", settings.stripe_secret_key
        active_webhook = bool(settings.stripe_webhook_secret)
    else:
        active_source, active_key, active_webhook = "none", "", False
    plans = db.scalars(select(SubscriptionPlan).order_by(
        SubscriptionPlan.is_active.desc(), SubscriptionPlan.monthly_price_cents)).all()
    last_event = db.scalar(select(func.max(StripeEvent.received_at)))
    row = _row(db)
    return {
        "enabled": bool(data.get("enabled", True)) if data else True,
        "saved": bool(data),
        "secret_key_hint": _hint(saved_secret),
        "secret_key_mode": mode_of(saved_secret),
        "has_webhook_secret": bool(saved_webhook),
        "active_source": active_source,
        "active_mode": mode_of(active_key),
        "active_has_webhook_secret": active_webhook,
        "environment_key_hint": _hint(settings.stripe_secret_key),
        "webhook_url": f"{settings.public_base_url.rstrip('/')}/api/v1/billing/webhook",
        "webhook_events": list(WEBHOOK_EVENTS),
        "last_event_at": last_event.isoformat() if last_event else None,
        "plans": [{"id": p.id, "name": p.name, "is_active": p.is_active,
                   "monthly_price_cents": p.monthly_price_cents,
                   "stripe_price_id": p.stripe_price_id} for p in plans],
        "updated_by": row.updated_by if row else None,
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }
