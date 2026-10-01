"""Deleting an account for good — by its owner, or by staff once deactivated.

Everything the account owns goes: connections and their credentials, devices,
punches, attendance, employee mappings, sync runs, users and their sessions,
its audit trail. What stays is one ``account_closure`` row (who, when, why)
and the ADMS device registrations, released (tenant cleared) so the same
terminal can be claimed by another account later.

Rows are deleted explicitly, child tables first, rather than relying on
``ON DELETE CASCADE``: SQLite only enforces foreign keys with a pragma this
app does not set, and an explicit list is also the clearest statement of what
"delete the account" removes.

A running Stripe subscription is cancelled first, immediately. If Stripe
can't be reached the deletion stops — an account must never be deleted while
it is still being charged.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    AccountClosure,
    AdmsDevice,
    AttendanceRecord,
    AuditLog,
    Device,
    DeviceSource,
    EmployeeMapping,
    OdooConnection,
    PendingSignup,
    PunchRecord,
    SyncRun,
    Tenant,
    User,
    UserRole,
    UserSession,
)
from app.services import billing

log = logging.getLogger(__name__)

#: Self-service exit reasons; "other" needs the free-text detail.
EXIT_REASONS = {
    "too_expensive": "Too expensive",
    "missing_feature": "Missing a feature we need",
    "device_unsupported": "Our biometric device isn't supported",
    "switching": "Switching to another product",
    "not_using": "Not using it enough",
    "technical_issues": "Technical problems",
    "temporary": "Only needed it temporarily",
    "other": "Other",
}

#: Child tables first. Every one of them carries ``tenant_id``.
_TENANT_TABLES = (AttendanceRecord, PunchRecord, EmployeeMapping, SyncRun, Device,
                  DeviceSource, OdooConnection, AuditLog, PendingSignup)


def delete_tenant(db: Session, tenant: Tenant, *, closed_by: str, closed_by_email: str | None,
                  reason_code: str | None, reason_text: str | None) -> AccountClosure:
    """Delete ``tenant`` and everything under it; returns the closure record.
    Commits. Raises billing.BillingError (nothing deleted) if Stripe refuses."""
    cancelled = False
    if tenant.stripe_subscription_id and billing.enabled():
        billing.cancel_subscription_now(tenant.stripe_subscription_id)
        cancelled = True

    owner_email = db.scalar(select(User.email).where(
        User.tenant_id == tenant.id, User.role == UserRole.owner.value).limit(1))
    closure = AccountClosure(
        id=uuid.uuid4().hex,
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        owner_email=owner_email,
        plan_name=tenant.plan.name if tenant.plan else None,
        status_before=tenant.status,
        closed_by=closed_by,
        closed_by_email=closed_by_email,
        reason_code=reason_code,
        reason_text=(reason_text or "").strip() or None,
        stripe_subscription_cancelled=cancelled,
    )
    db.add(closure)

    user_ids = select(User.id).where(User.tenant_id == tenant.id)
    db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
    db.execute(update(AdmsDevice).where(AdmsDevice.tenant_id == tenant.id)
               .values(tenant_id=None, source_id=None))
    for model in _TENANT_TABLES:
        db.execute(delete(model).where(model.tenant_id == tenant.id))
    # A staff member's own login can't belong to a customer account being
    # deleted out from under them — but a staff flag on a tenant user is
    # possible; it goes with the account like every other user.
    db.execute(delete(User).where(User.tenant_id == tenant.id))
    db.execute(delete(Tenant).where(Tenant.id == tenant.id))
    db.commit()
    log.warning("Account %s (%s) deleted by %s %s — reason: %s %s", closure.tenant_slug, closure.tenant_id,
                closed_by, closed_by_email, reason_code, (closure.reason_text or "")[:200])
    return closure
