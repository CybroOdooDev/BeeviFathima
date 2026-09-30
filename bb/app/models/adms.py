"""ZKTeco ADMS ("cloud server" / PUSH) devices and their command queue.

A push terminal is identified by nothing but its serial number, which it
sends on every request. ``AdmsDevice`` is the platform-wide claim on that
serial: one row per terminal that has ever called in, claimed by at most one
tenant's connection. A serial nobody has claimed yet is still recorded (so
"Test connection" can say "yes, we can hear it") but its punches are refused
until it is claimed — refused, not dropped, so the terminal keeps them and
sends them again once it is.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped, UUIDPk


class AdmsDevice(Base, UUIDPk, Timestamped):
    __tablename__ = "adms_device"

    serial_number: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    #: Null until a tenant adds a push connection with this serial.
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenant.id", ondelete="SET NULL"), index=True
    )
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("device_source.id", ondelete="SET NULL"), index=True
    )

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ip: Mapped[str | None] = mapped_column(String(64))
    push_version: Mapped[str | None] = mapped_column(String(32))
    firmware: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(80))
    #: Counts the terminal reports in its heartbeat (users, fingerprints, logs).
    user_count: Mapped[int | None] = mapped_column(Integer)
    attlog_count: Mapped[int | None] = mapped_column(Integer)
    #: The last ATTLOG / OPERLOG stamp stored — handed back at handshake so the
    #: terminal resumes from there instead of re-sending its whole log.
    attlog_stamp: Mapped[str | None] = mapped_column(String(32))
    operlog_stamp: Mapped[str | None] = mapped_column(String(32))
    #: Users the terminal has told us about (PIN -> name), from its user
    #: uploads, plus ones we have queued to create. What "already on the
    #: device" means for provisioning.
    users: Mapped[dict | None] = mapped_column(JSON, default=dict)


class AdmsCommand(Base):
    """One command waiting for (or answered by) a terminal.

    Handed out on the terminal's next ``/iclock/getrequest`` as
    ``C:<id>:<command>``; the terminal reports the result to
    ``/iclock/devicecmd``. Integer ids because that is what the protocol
    carries.
    """

    __tablename__ = "adms_command"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    #: queued -> sent -> done | failed
    status: Mapped[str] = mapped_column(String(10), default="queued", nullable=False)
    return_code: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
