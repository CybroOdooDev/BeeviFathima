"""Tenant, users and sessions."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamped, UUIDPk


class TenantStatus(str, enum.Enum):
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    suspended = "suspended"
    cancelled = "cancelled"


class UserRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    viewer = "viewer"


class Tenant(Base, UUIDPk, Timestamped):
    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=TenantStatus.trialing.value)

    #: The tenant's own timezone, used to render reports. Not the BioTime
    #: server's timezone -- that lives on the source, because a tenant can have
    #: sites in different zones.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- pairing policy, tenant-wide defaults --------------------------------
    pairing_mode: Mapped[str] = mapped_column(String(20), default="alternating")
    day_boundary_hour: Mapped[int] = mapped_column(Integer, default=4)
    min_punch_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    max_shift_hours: Mapped[int] = mapped_column(Integer, default=16)
    orphan_out_policy: Mapped[str] = mapped_column(String(20), default="flag")
    auto_create_employees: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- working hours, for late-arrival scoring -----------------------------
    work_start_time: Mapped[str] = mapped_column(String(5), default="09:00")
    late_grace_minutes: Mapped[int] = mapped_column(Integer, default=10)

    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    users: Mapped[list["User"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    @property
    def crypto_key(self) -> str:
        """Salt for deriving this tenant's credential-encryption key."""
        return self.id


class User(Base, UUIDPk, Timestamped):
    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default=UserRole.owner.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship(back_populates="users")


class UserSession(Base, UUIDPk, Timestamped):
    """One row per issued refresh token, so a login is visible and revocable.

    The token itself is never stored -- only the sha256 of its ``jti``. A
    database dump therefore yields no usable session.
    """

    __tablename__ = "user_session"
    __table_args__ = (Index("ix_session_user_revoked", "user_id", "revoked_at"),)

    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_label: Mapped[str | None] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(80))

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class AuditLog(Base, UUIDPk, Timestamped):
    __tablename__ = "audit_log"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(64))
