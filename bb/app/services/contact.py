"""Contact / Book a demo: save the request, tell sales, acknowledge the visitor.

The request is written to the database *first*, so a mail problem can never lose
a lead; the emails are best-effort after that.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ContactRequest, User
from app.services.mailer import send_email

log = logging.getLogger(__name__)

# --- rate limit: per IP, in-process (same trade-off as registration's) --------
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def allow(ip: str | None) -> bool:
    limit = settings.contact_rate_per_hour
    if limit <= 0:
        return True
    now = time.monotonic()
    with _lock:
        q = _hits[ip or "?"]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def sales_recipients(db: Session) -> list[str]:
    if settings.sales_email.strip():
        return [e.strip() for e in settings.sales_email.split(",") if e.strip()]
    return list(db.scalars(select(User.email).where(
        User.is_platform_admin.is_(True), User.is_active.is_(True))).all())


def _line(label: str, value: str | None) -> str:
    return f"{label}: {value}" if value else ""


def notify_sales(db: Session, req: ContactRequest) -> None:
    to = sales_recipients(db)
    if not to:
        log.warning("Contact request %s saved, but nobody is set to be told (SALES_EMAIL / staff)", req.id)
        return
    when = (f"{req.preferred_date} · {req.preferred_window or 'any time'} ({req.timezone or 'timezone unknown'})"
            if req.preferred_date else None)
    fields = [
        _line("Preferred time", when), _line("Phone", req.phone), _line("Employees", req.employees),
        _line("Odoo version", req.odoo_version), _line("Odoo hosting", req.odoo_hosting),
        _line("Biometric system", req.biometric_system), _line("Device setup", req.device_setup),
    ]
    body = f"{req.name} <{req.email}> at {req.company} sent a {req.topic.lower()} request.\n\n"
    body += "\n".join(f for f in fields if f)
    if req.message:
        body += f"\n\nMessage:\n{req.message}"
    body += f"\n\nReply to {req.email}. It is also in the staff console under Leads."
    subject = f"[BioBridge] {req.topic}: {req.name}, {req.company}"
    for address in to:
        try:
            send_email(address, subject, body, db=db)
        except Exception as exc:  # noqa: BLE001 — the lead is already saved
            log.warning("Could not tell %s about contact request %s: %s", address, req.id, exc)


def acknowledge_visitor(db: Session, req: ContactRequest) -> None:
    """A short receipt. Sent only to the address typed on the form, which is why
    the endpoint is rate limited and honeypotted."""
    booking = (f"\nIf you'd rather pick a time yourself: {settings.demo_booking_url}\n"
               if settings.demo_booking_url and req.topic == "Demo" else "")
    body = (
        f"Hi {req.name.split()[0]},\n\n"
        f"Thanks for getting in touch about BioBridge — we've got your {req.topic.lower()} request"
        + (f" for {req.preferred_date}" + (f" ({req.preferred_window.lower()})" if req.preferred_window else "")
           if req.preferred_date else "")
        + " and will reply within one working day.\n"
        + booking
        + "\nIf anything changes, just reply to this email.\n\n— The BioBridge team\n"
    )
    try:
        send_email(req.email, "We've got your BioBridge request", body, db=db)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not acknowledge contact request %s: %s", req.id, exc)
