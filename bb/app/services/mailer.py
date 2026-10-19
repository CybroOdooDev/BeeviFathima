"""The one place anything is mailed from.

Which server is used is decided per send by app.services.mail_settings: the
console's Email server settings first, then ``SMTP_*`` from the environment.
With neither configured — the default in development and in every test — the
message is logged instead of sent, so a fresh checkout works with zero setup
and a developer can read a verification link straight out of the console.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from sqlalchemy.orm import Session

from app.services.mail_settings import MailConfig, active_config

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


class MailError(RuntimeError):
    """A send failed; the message is written for the person configuring SMTP."""


def _explain(exc: Exception, config: MailConfig) -> str:
    gmail = "gmail" in config.host or "googlemail" in config.host
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        who = config.username or config.from_email
        if gmail:
            return (f"Google refused the sign-in as {who!r}. The Username must be the Gmail / Workspace "
                    "address the App Password was created on (usually the same as the From address). "
                    "Use an App Password (16 characters, from "
                    "Google Account → Security → App passwords) — the normal password doesn't "
                    "work for SMTP.")
        return f"The server refused the username or password ({exc.smtp_code})."
    if isinstance(exc, smtplib.SMTPSenderRefused) and (
            exc.smtp_code == 530 or b"authentication required" in (exc.smtp_error or b"").lower()):
        return ("The server needs a sign-in before it will send, but none was made. Fill in the Username "
                + ("(the full Gmail / Workspace address) and an App Password" if gmail else "and Password")
                + " in Email server settings and save.")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"The server refused the From address {config.from_email!r}: {exc.smtp_error.decode(errors='replace')}"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The server refused the recipient address."
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "The server doesn't support that security setting — try STARTTLS on 587 or SSL on 465."
    if isinstance(exc, (ssl.SSLError, smtplib.SMTPServerDisconnected)):
        return ("The secure connection failed. Use STARTTLS with port 587, or SSL/TLS with port 465 — "
                "the security setting has to match the port.")
    if isinstance(exc, (TimeoutError, OSError)):
        return (f"Could not reach {config.host}:{config.port} ({type(exc).__name__}). Check the host and "
                "port, and that this server is allowed outbound connections on that port.")
    return f"Sending failed: {exc}"


def requires_login(config: MailConfig) -> bool:
    """Providers that never relay without authentication."""
    host = config.host.lower()
    return any(h in host for h in ("gmail.com", "googlemail.com", "office365.com", "outlook.com"))


def build_message(config: MailConfig, to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    sender = config.from_email or config.username
    message["From"] = formataddr((config.from_name, sender)) if config.from_name else sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    domain = sender.rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    if config.reply_to:
        message["Reply-To"] = config.reply_to
    message.set_content(body)
    return message


def deliver(config: MailConfig, message: EmailMessage) -> None:
    """Send through ``config``; raises MailError with a readable reason."""
    # A password with no username: sign in as the From address, which is what
    # Gmail / Workspace and most hosted providers expect.
    login = config.username or (config.from_email if config.password else "")
    if requires_login(config) and not (login and config.password):
        raise MailError("Gmail only sends after signing in: fill in the Username (the full Gmail / Workspace "
                        "address) and an App Password in Email server settings, then save.")
    try:
        context = ssl.create_default_context()
        if config.security == "ssl":
            client = smtplib.SMTP_SSL(config.host, config.port, timeout=TIMEOUT_SECONDS, context=context)
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=TIMEOUT_SECONDS)
        with client:
            if config.security == "starttls":
                client.starttls(context=context)
            if login and config.password:
                client.login(login, config.password)
            client.send_message(message)
    except MailError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MailError(_explain(exc, config)) from exc


def send_email(to: str, subject: str, body: str, db: Session | None = None) -> None:
    """Best-effort by design: callers decide whether a failure here should
    stop what they were doing. Signup and resend-verification both choose
    not to — a customer's account creation must not fail because SMTP is
    down, only their inbox stays unconfirmed until a retry succeeds.

    ``db`` lets the console's saved settings be read; without it only the
    environment settings apply.
    """
    config = active_config(db)
    if not config.can_send:
        log.info("MAIL (no SMTP configured) to=%s subject=%r\n%s", to, subject, body)
        return
    deliver(config, build_message(config, to, subject, body))
