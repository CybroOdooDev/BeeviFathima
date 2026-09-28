"""The one place anything is mailed from.

No SMTP configured — the default in development and in every test — logs
the message instead of sending it. Same shape as ``REDIS_URL`` empty meaning
"no broker, run inline": a fresh checkout works with zero setup, and a
developer can read a verification link straight out of the console instead
of standing up a mail server.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> None:
    """Best-effort by design: callers decide whether a failure here should
    stop what they were doing. Signup and resend-verification both choose
    not to — a customer's account creation must not fail because SMTP is
    down, only their inbox stays unconfirmed until a retry succeeds.
    """
    if not settings.smtp_host:
        log.info("MAIL (no SMTP configured) to=%s subject=%r\n%s", to, subject, body)
        return

    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        if settings.smtp_use_tls:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)
