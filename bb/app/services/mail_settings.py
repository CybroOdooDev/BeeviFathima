"""Where outbound mail goes: the console's Email server settings, or .env.

Order of precedence, checked on every send so a change in the console takes
effect immediately on every process:

1. The ``mail`` row in ``platform_setting``, when staff have saved one and
   left it enabled.
2. The ``SMTP_*`` / ``MAIL_FROM`` environment settings, when ``SMTP_HOST``
   is set — so an existing deployment keeps working with no console step.
3. Nothing: mail is logged, not sent (development and tests).

The SMTP password is encrypted with a key derived for the platform (the same
envelope scheme as tenant credentials), and never returned by the API.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import CryptoError, decrypt, encrypt
from app.models import PlatformSetting

log = logging.getLogger(__name__)

KEY = "mail"
#: The HKDF "tenant" label for platform secrets — never a real tenant key.
CRYPTO_KEY = "platform:mail"

SECURITY_CHOICES = ("starttls", "ssl", "none")

#: Known-good values the console can fill in. Only Gmail is offered as a
#: one-click preset for now; anything else is typed by hand.
PRESETS = {
    "gmail": {
        "label": "Google Workspace / Gmail",
        "host": "smtp.gmail.com",
        "port": 587,
        "security": "starttls",
        "help": (
            "Sign in with the full Google address and a 16-character App Password "
            "(Google Account → Security → 2-Step Verification → App passwords), "
            "not the normal password. The From address must be that account or "
            "one of its verified 'Send mail as' aliases, or Google rewrites it."
        ),
    },
}


@dataclass
class MailConfig:
    source: str            # "database", "environment" or "none"
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    security: str = "starttls"
    from_email: str = ""
    from_name: str = ""
    reply_to: str = ""
    enabled: bool = True

    @property
    def can_send(self) -> bool:
        return self.source != "none" and bool(self.host)


def _row(db: Session) -> PlatformSetting | None:
    return db.get(PlatformSetting, KEY)


def _stored(db: Session) -> dict:
    row = _row(db)
    if row is None:
        return {}
    try:
        return json.loads(row.value or "{}")
    except ValueError:
        log.error("platform_setting 'mail' is not valid JSON — ignoring it")
        return {}


def from_environment() -> MailConfig:
    if not settings.smtp_host:
        return MailConfig(source="none", from_email=settings.mail_from)
    return MailConfig(
        source="environment",
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        security="starttls" if settings.smtp_use_tls else ("ssl" if settings.smtp_port == 465 else "none"),
        from_email=settings.mail_from,
    )


def stored_config(db: Session) -> MailConfig | None:
    """What the console saved, decrypted — or None when nothing is saved."""
    data = _stored(db)
    if not data:
        return None
    try:
        password = decrypt(data.get("password_enc"), CRYPTO_KEY) or ""
    except CryptoError:
        log.error("The saved SMTP password cannot be decrypted (MASTER_ENCRYPTION_KEY changed?)")
        password = ""
    return MailConfig(
        source="database",
        host=data.get("host") or "",
        port=int(data.get("port") or 587),
        username=data.get("username") or "",
        password=password,
        security=data.get("security") if data.get("security") in SECURITY_CHOICES else "starttls",
        from_email=data.get("from_email") or "",
        from_name=data.get("from_name") or "",
        reply_to=data.get("reply_to") or "",
        enabled=bool(data.get("enabled", True)),
    )


def active_config(db: Session | None) -> MailConfig:
    """The configuration a send should use right now."""
    if db is not None:
        try:
            saved = stored_config(db)
        except Exception as exc:  # noqa: BLE001 — a missing table must not stop mail
            log.warning("Could not read console mail settings, using .env: %s", exc)
            saved = None
        if saved and saved.enabled and saved.host:
            return saved
    return from_environment()


def save(db: Session, data: dict, actor_email: str | None) -> MailConfig:
    """Merge ``data`` into the saved settings. A blank or missing password
    keeps the one already stored; ``clear_password`` removes it."""
    current = _stored(db)
    merged = {k: v for k, v in current.items()}
    for key in ("host", "port", "username", "security", "from_email", "from_name", "reply_to", "enabled"):
        if key in data and data[key] is not None:
            value = data[key]
            merged[key] = value.strip() if isinstance(value, str) else value
    if data.get("clear_password"):
        merged.pop("password_enc", None)
    elif data.get("password"):
        # Gmail shows App Passwords in groups of four; the spaces aren't part of it.
        secret = data["password"].replace(" ", "") if "gmail" in merged.get("host", "") else data["password"]
        merged["password_enc"] = encrypt(secret, CRYPTO_KEY)
    row = _row(db)
    if row is None:
        row = PlatformSetting(key=KEY)
        db.add(row)
    row.value = json.dumps(merged)
    row.updated_by = actor_email
    db.commit()
    return stored_config(db)


def public_view(db: Session) -> dict:
    """What the console shows — never the password itself."""
    saved = stored_config(db)
    env = from_environment()
    active = active_config(db)
    row = _row(db)
    view = asdict(saved) if saved else asdict(MailConfig(source="database", enabled=True))
    view.pop("password", None)
    view.pop("source", None)
    view.update(
        has_password=bool(saved and saved.password),
        saved=saved is not None,
        active_source=active.source,
        environment={"host": env.host, "port": env.port, "from_email": env.from_email} if env.source != "none" else None,
        updated_by=row.updated_by if row else None,
        updated_at=row.updated_at.isoformat() if row and row.updated_at else None,
        presets=PRESETS,
    )
    return view
