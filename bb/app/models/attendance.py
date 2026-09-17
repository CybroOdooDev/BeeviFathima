"""Employee mapping, the punch ledger, resolved attendance and run history."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamped, UUIDPk


class MappingStatus(str, enum.Enum):
    mapped = "mapped"
    unmapped = "unmapped"
    ignored = "ignored"
    ambiguous = "ambiguous"


class Direction(str, enum.Enum):
    """Normalised punch direction, after interpreting the vendor's own coding."""

    inward = "in"
    outward = "out"
    unknown = "unknown"


class PunchState(str, enum.Enum):
    pending = "pending"     # ingested, not yet turned into attendance
    synced = "synced"       # reflected in an Odoo attendance record
    skipped = "skipped"     # deduplicated, or the employee is ignored
    unmapped = "unmapped"   # no Odoo employee carries this badge yet
    error = "error"         # the push failed; retried while attempts remain


class SyncStatus(str, enum.Enum):
    running = "running"
    success = "success"
    partial = "partial"
    failed = "failed"


class EmployeeMapping(Base, UUIDPk, Timestamped):
    """``emp_code`` (the badge) ⇄ ``hr.employee.id`` (Odoo), resolved once."""

    __tablename__ = "employee_mapping"
    __table_args__ = (
        UniqueConstraint("tenant_id", "emp_code", name="uq_mapping_emp_code"),
        Index("ix_mapping_tenant_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Text, not an integer: badges carry leading zeros and letters, and an
    #: integer column silently turns "0042" into 42 and rejects "A7".
    emp_code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(200))

    odoo_employee_id: Mapped[int | None] = mapped_column(Integer)
    odoo_employee_name: Mapped[str | None] = mapped_column(String(200))

    status: Mapped[str] = mapped_column(String(20), default=MappingStatus.unmapped.value)
    match_method: Mapped[str | None] = mapped_column(String(32))
    match_note: Mapped[str | None] = mapped_column(Text)

    #: The shift currently open in Odoo for this person, if any. Carrying it
    #: here is what lets a check-out arriving in a later cycle close the record
    #: its check-in opened, instead of being re-paired on its own.
    open_attendance_id: Mapped[int | None] = mapped_column(Integer)
    open_check_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    last_punch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PunchRecord(Base, UUIDPk, Timestamped):
    """Immutable ledger of every punch pulled from any attendance platform.

    Identity is ``(tenant_id, source_id, external_id)``: the vendor's own id for
    the event, scoped to the source it came from. That is what makes the fetch
    step replayable — an overlapping window and a full re-pull are both no-ops —
    without assuming ids are unique across vendors, or even numeric.
    """

    __tablename__ = "punch_record"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", "external_id", name="uq_punch_external"),
        Index("ix_punch_tenant_state", "tenant_id", "process_state"),
        Index("ix_punch_tenant_emp_time", "tenant_id", "emp_code", "punch_time_utc"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(32))

    external_id: Mapped[str] = mapped_column(String(190), nullable=False)
    emp_code: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Naive UTC, matching how Odoo stores Datetime fields.
    punch_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    #: Kept for support: "the device says 08:00, we stored 04:00, here is why".
    punch_time_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    direction: Mapped[str] = mapped_column(String(10), default=Direction.unknown.value)
    raw_state: Mapped[str | None] = mapped_column(String(8))
    verify_type: Mapped[str | None] = mapped_column(String(8))
    terminal_sn: Mapped[str | None] = mapped_column(String(64), index=True)

    process_state: Mapped[str] = mapped_column(String(16), default=PunchState.pending.value)
    odoo_attendance_id: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    raw: Mapped[dict | None] = mapped_column(JSON)


class AttendanceRecord(Base, UUIDPk, Timestamped):
    """A resolved interval, mirrored locally.

    The ledger alone cannot answer "show me last week's hours" without re-running
    the pairing algorithm, and querying the customer's Odoo on every page load
    would be slow and would spend their rate budget. So the engine writes what it
    pushed, and every report reads from here.
    """

    __tablename__ = "attendance_record"
    __table_args__ = (
        UniqueConstraint("tenant_id", "odoo_attendance_id", name="uq_attendance_odoo_id"),
        Index("ix_attendance_tenant_checkin", "tenant_id", "check_in"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    emp_code: Mapped[str] = mapped_column(String(64), nullable=False)
    employee_name: Mapped[str | None] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(200))

    odoo_employee_id: Mapped[int | None] = mapped_column(Integer)
    odoo_attendance_id: Mapped[int | None] = mapped_column(Integer)

    check_in: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    check_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    worked_hours: Mapped[float | None] = mapped_column(Float)

    #: Local-time helpers so reports never reconvert per row.
    shift_date: Mapped[str | None] = mapped_column(String(10), index=True)
    check_in_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    check_out_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    device_serial: Mapped[str | None] = mapped_column(String(64))
    pairing_mode: Mapped[str | None] = mapped_column(String(20))

    is_auto_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_orphan_out: Mapped[bool] = mapped_column(Boolean, default=False)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    late_minutes: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)


class SyncRun(Base, UUIDPk, Timestamped):
    __tablename__ = "sync_run"
    __table_args__ = (Index("ix_run_tenant_started", "tenant_id", "started_at"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default=SyncStatus.running.value)
    triggered_by: Mapped[str] = mapped_column(String(20), default="schedule")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    cursor_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    cursor_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    punches_fetched: Mapped[int] = mapped_column(Integer, default=0)
    punches_new: Mapped[int] = mapped_column(Integer, default=0)
    punches_skipped: Mapped[int] = mapped_column(Integer, default=0)
    attendances_created: Mapped[int] = mapped_column(Integer, default=0)
    attendances_closed: Mapped[int] = mapped_column(Integer, default=0)
    employees_matched: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[str | None] = mapped_column(Text)
    log: Mapped[list | None] = mapped_column(JSON)
