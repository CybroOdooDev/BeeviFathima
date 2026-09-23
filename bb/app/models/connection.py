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
    text,
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
    #: Set by the connection probe when hr.attendance carries device_id — i.e.
    #: the optional biobridge_attendance Odoo add-on (odoo_addon/) is
    #: installed there. Gates OdooClient.upsert_device()/create_attendance's
    #: device_id: calling either against a plain Odoo, where the model and
    #: field don't exist, would just raise. server_default alongside the
    #: Python-side default — see connection_kind above for why both are
    #: needed for a NOT NULL column added to a table that may already exist.
    has_device_tracking: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: "module" (odoo_addon/biobridge_attendance/ installed — Odoo.sh/
    #: self-hosted only) or "bootstrap" (BioBridge created x_biobridge_device
    #: + x_device_id/x_device_location itself, purely over the external API —
    #: see OdooClient.ensure_device_tracking_bootstrap; the path that reaches
    #: Odoo Online). Null when has_device_tracking is False. Nullable rather
    #: than NOT NULL-with-default: unlike has_device_tracking, "no mode yet"
    #: genuinely has no sensible non-null value to backfill existing rows
    #: with, and the column is only ever read alongside has_device_tracking.
    device_tracking_mode: Mapped[str | None] = mapped_column(String(20))

    #: The res.company id this connection is pinned to, or null for "every
    #: company the Odoo user can see" — fine for a single-company Odoo,
    #: dangerous for a multi-company one shared across several BioBridge
    #: tenants (each tenant's own DeviceSource rows are already isolated by
    #: tenant_id; this is what isolates the Odoo side of the same tenant's
    #: connection). See OdooClient.execute's allowed_company_ids injection.
    #: Nullable, no server_default: unlike has_device_tracking there is no
    #: safe non-null value to backfill an existing single-company connection
    #: with — null already means exactly what those rows need.
    company_id: Mapped[int | None] = mapped_column(Integer)
    #: Display cache only — filled from Test Connection's company list, never
    #: authoritative, never read by anything that makes a security decision.
    company_name: Mapped[str | None] = mapped_column(String(120))

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

    #: "platform" or "device" — purely how this connection is framed to the
    #: tenant (a shared server vs. one standalone terminal). Both values use
    #: the exact same integration underneath; nothing here branches on it.
    #: A separate wire protocol for standalone devices is a future addition,
    #: not this field's job — see app/integrations/base.py.
    #:
    #: ``server_default`` as well as ``default``: this is a NOT NULL column
    #: added after the table already existed, so tools/migrate.py needs a
    #: default it can put in the ALTER TABLE itself, to backfill the rows
    #: already there — the Python-side default only applies to new inserts.
    connection_kind: Mapped[str] = mapped_column(
        String(20), default="platform", server_default=text("'platform'"), nullable=False
    )

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

    #: Mirrors tenant.auto_create_employees but runs in the opposite
    #: direction and is scoped to this one source: when on, the sync engine
    #: pushes Odoo's roster at this platform/device, creating a provider-side
    #: user for any Odoo employee it can't find there (see
    #: app.services.sync_engine's reconciliation stage). Off by default —
    #: provisioning identities onto a customer's biometric estate is not
    #: something to start doing silently the moment this column exists.
    #:
    #: server_default alongside default for the same reason as
    #: connection_kind above: this is a NOT NULL column added to a table
    #: that may already have rows, so tools/migrate.py's ALTER TABLE needs a
    #: DDL-level default to backfill them with.
    auto_provision_employees: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

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
