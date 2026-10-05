"""Email the account when something serious has been wrong for a while.

The in-app bell only helps someone who has the app open. This looks at the same
list (app.services.alerts) on a timer and mails the owner and admins about the
*red* alerts, with three rules so it never becomes noise:

* a problem has to stand for ``alert_email_grace_minutes`` before anyone hears
  about it — a blip that clears itself is not worth an email;
* one problem is one email, then a reminder at most every
  ``alert_email_reminder_hours`` while it stays unresolved;
* an alert that clears is forgotten, so if it comes back it is news again.

Amber alerts (unmatched badges, a terminal that went quiet) are for the bell
only. The "account stopped" alert is skipped: BioBridge already emails that
itself, in its own words, at the moment it stops the account.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Tenant, TenantStatus, User, UserRole
from app.services.alerts import BAD, tenant_alerts
from app.services.mailer import send_email
from app.services.timeutils import ensure_aware

log = logging.getLogger(__name__)

#: Accounts that are meant to be syncing. Suspended / cancelled ones were
#: stopped on purpose, and the customer was told so.
WATCHED = (TenantStatus.trialing.value, TenantStatus.active.value, TenantStatus.past_due.value)
NOT_EMAILED = {"account-stopped"}


def _parse(value: str | None) -> datetime | None:
    return ensure_aware(datetime.fromisoformat(value)) if value else None


def due_alerts(state: dict, alerts: list[dict], now: datetime) -> tuple[list[dict], dict, list[dict]]:
    """Update ``state`` for the current alerts; return (state, to_email, reminders).

    Pure, so the timing rules can be tested without a database or a clock."""
    grace = timedelta(minutes=settings.alert_email_grace_minutes)
    remind = timedelta(hours=settings.alert_email_reminder_hours)
    live = {a["key"]: a for a in alerts if a["severity"] == BAD and a["key"] not in NOT_EMAILED}
    new_state: dict = {}
    fresh, reminders = [], []
    for key, alert in live.items():
        entry = dict(state.get(key) or {"first_seen": now.isoformat(), "emailed_at": None})
        first = _parse(entry["first_seen"]) or now
        emailed = _parse(entry.get("emailed_at"))
        if emailed is None:
            if now - first >= grace:
                fresh.append(alert)
                entry["emailed_at"] = now.isoformat()
        elif now - emailed >= remind:
            reminders.append(alert)
            entry["emailed_at"] = now.isoformat()
        new_state[key] = entry
    return fresh, new_state, reminders


def _body(tenant: Tenant, fresh: list[dict], reminders: list[dict]) -> tuple[str, str]:
    from app.services.onboarding import login_url

    items = fresh + reminders
    lines = []
    for a in items:
        lines.append(f"• {a['title']}\n  {a['detail']}")
    only_reminders = not fresh
    subject = (f"Still unresolved: {len(items)} problem{'s' if len(items) != 1 else ''} with BioBridge ({tenant.name})"
               if only_reminders else
               f"BioBridge needs attention ({tenant.name}): {items[0]['title']}"
               + (f" (+{len(items) - 1} more)" if len(items) > 1 else ""))
    body = (
        "Hello,\n\n"
        + ("These problems with your BioBridge account are still unresolved:\n\n" if only_reminders
           else "BioBridge found a problem that is keeping attendance from reaching Odoo:\n\n")
        + "\n\n".join(lines)
        + "\n\nNothing is lost — punches are kept and sent as soon as it is fixed.\n\n"
        f"Open BioBridge: {login_url()}  (the bell at the top shows the same list)\n\n"
        "You can switch these emails off under Settings → General.\n\n— BioBridge\n"
    )
    return subject, body


def sweep_alert_emails(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    sent = tenants = 0
    for tenant in db.scalars(select(Tenant).where(Tenant.status.in_(WATCHED))).all():
        try:
            if not tenant.alert_emails_enabled:
                if tenant.alert_state:
                    tenant.alert_state = {}
                    db.commit()
                continue
            fresh, state, reminders = due_alerts(tenant.alert_state or {}, tenant_alerts(db, tenant, now=now), now)
            if fresh or reminders:
                recipients = db.scalars(select(User.email).where(
                    User.tenant_id == tenant.id, User.is_active.is_(True),
                    User.role.in_([UserRole.owner.value, UserRole.admin.value]))).all()
                subject, body = _body(tenant, fresh, reminders)
                for email in recipients:
                    try:
                        send_email(email, subject, body, db=db)
                        sent += 1
                    except Exception as exc:  # noqa: BLE001 — one bad address mustn't stop the rest
                        log.warning("Could not send the alert email to %s: %s", email, exc)
                tenants += 1
            if state != (tenant.alert_state or {}):
                tenant.alert_state = state
            db.commit()
        except Exception:  # noqa: BLE001 — one tenant must not stop the sweep
            db.rollback()
            log.exception("Alert email sweep failed for %s", tenant.slug)
    return {"tenants_emailed": tenants, "emails_sent": sent}
