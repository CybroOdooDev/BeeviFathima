"""The customer's own endpoints: their Odoo, and their attendance platforms."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamped, UUIDPk


class ConnectionStatus(str, enum.Enum):
    unverified = "unverified"
    connected = "connected"
    degraded = "degraded"
    failed = "failed"


class OdooConnection(Base, UUIDPk, Timestamped):
    __tablename__ = "odoo_connection"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_odoo_conn_name"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), default="Primary Odoo")
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    db_name: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_enc: Mapped[str | None] = mapped_column(Text)

    odoo_version: Mapped[str | None] = mapped_column(String(32))
    #: Saves one authenticate round-trip per run.
    uid_cache: Mapped[int | None] = mapped_column(Integer)
    #: Set by the connection probe when hr.attendance carries biotime_ref.
    has_companion_addon: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(20), default=ConnectionStatus.unverified.value)
    status_message: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class DeviceSource(Base, UUIDPk, Timestamped):
    """One attendance platform belonging to one tenant.

    A tenant may hold several, of different vendors: two BioTime servers across
    sites, or a BioTime server plus another vendor's REST API. ``provider``
    selects the integration; ``config`` carries whatever that integration needs
    beyond the shared fields, which is what lets a new vendor ship without a
    database migration.
    """

    __tablename__ = "device_source"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_source_name"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), default="Primary BioTime")
    #: Registry slug — see app/integrations/providers/.
    provider: Mapped[str] = mapped_column(String(40), default="biotime", nullable=False)
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)

    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_enc: Mapped[str | None] = mapped_column(Text)
    auth_type: Mapped[str] = mapped_column(String(10), default="token")
    token_enc: Mapped[str | None] = mapped_column(Text)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)

    #: The zone the platform's own clock runs in. Punch times arrive as naive
    #: local wall-clock, so this is the only thing that makes them meaningful.
    server_timezone: Mapped[str] = mapped_column(String(64), default="UTC")

    #: High-water mark: naive UTC time of the newest punch ingested. Advanced
    #: only after punches are stored, so a crash costs a retry, never data.
    cursor_punch_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    status: Mapped[str] = mapped_column(String(20), default=ConnectionStatus.unverified.value)
    status_message: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    devices: Mapped[list["Device"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Device(Base, UUIDPk, Timestamped):
    __tablename__ = "device"
    __table_args__ = (
        UniqueConstraint("tenant_id", "serial_number", name="uq_device_serial"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("device_source.id", ondelete="CASCADE"), index=True, nullable=False
    )

    serial_number: Mapped[str] = mapped_column(String(64), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(120))
    area: Mapped[str | None] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(80))

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Overrides the tenant default for punches from this terminal. Unlike the
    #: original implementation, the engine actually reads this.
    pairing_override: Mapped[str | None] = mapped_column(String(20))

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    punch_count: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped[DeviceSource] = relationship(back_populates="devices")
