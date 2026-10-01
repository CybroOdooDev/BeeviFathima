"""Platform-wide settings that staff change from the console.

A small key/value table rather than one column per setting, so a new console
setting needs no schema change (``create_all`` adds tables, never columns).
Each row holds one JSON document; secrets inside it are encrypted by the
service that owns the key (see app.services.mail_settings), never stored plain.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped


class PlatformSetting(Base, Timestamped):
    __tablename__ = "platform_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: JSON text. Secrets in it are already encrypted.
    value: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    #: Who last saved it — diagnostic, shown in the console.
    updated_by: Mapped[str | None] = mapped_column(String(254))


class AccountClosure(Base):
    """What is left of a deleted account: who it was, who closed it, and why.

    Written in the same transaction that deletes the tenant, and deliberately
    without a foreign key — the tenant row is gone. Holds no attendance data,
    no credentials, no employees: only enough for staff to answer "why did
    they leave" and "did we close that account" later.
    """

    __tablename__ = "account_closure"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    tenant_name: Mapped[str] = mapped_column(String(200))
    tenant_slug: Mapped[str | None] = mapped_column(String(120))
    owner_email: Mapped[str | None] = mapped_column(String(254))
    plan_name: Mapped[str | None] = mapped_column(String(80))
    status_before: Mapped[str | None] = mapped_column(String(20))
    #: "customer" (self-service) or "staff" (console).
    closed_by: Mapped[str] = mapped_column(String(20))
    closed_by_email: Mapped[str | None] = mapped_column(String(254))
    reason_code: Mapped[str | None] = mapped_column(String(40))
    reason_text: Mapped[str | None] = mapped_column(Text)
    stripe_subscription_cancelled: Mapped[bool] = mapped_column(default=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
