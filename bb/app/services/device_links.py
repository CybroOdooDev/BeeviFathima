"""Which terminal a punch came from, and linking Odoo attendance to it.

A punch is linked to its terminal (``PunchRecord.device_id``) when it is
fetched — but only if that terminal was already imported into BioBridge at
the time. A terminal imported afterwards left its earlier punches unlinked
for good, so the attendance records they became went to Odoo with no device,
even though every punch carries the terminal's serial number
(``terminal_sn``). ``device_for_punch`` falls back to that serial.

``link_attendance_devices`` fixes records already in Odoo: for every
``hr.attendance`` BioBridge created, it finds the terminal from the
record's earliest traceable punch (the check-in, normally) and writes it
onto the Odoo record over XML-RPC — only where Odoo's record has no device
yet, so a device someone set by hand in Odoo is never overwritten.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.odoo import OdooClient, OdooError
from app.models import Device, PunchRecord, Tenant


def device_for_punch(db: Session, punch: PunchRecord) -> Device | None:
    """The terminal a punch came from, or None if it can't be told.

    Uses the link made at fetch time when there is one; otherwise looks the
    terminal up by this punch's source and serial number (terminal identity
    is per source — see Device's unique constraint) and saves the link so
    the lookup happens once.
    """
    if punch.device_id:
        return db.get(Device, punch.device_id)
    if not punch.terminal_sn:
        return None
    device = db.scalar(
        select(Device).where(
            Device.source_id == punch.source_id,
            Device.serial_number == punch.terminal_sn,
        )
    )
    if device is not None:
        punch.device_id = device.id
    return device


@dataclass
class LinkReport:
    #: Odoo attendance records BioBridge created (from the punch ledger).
    attendances: int = 0
    #: ...of which none of the punches can be traced to a terminal.
    no_terminal: int = 0
    #: ...that already have a device in Odoo, or no longer exist there.
    already_linked: int = 0
    #: serial number -> attendance ids still needing a device.
    missing: dict[str, list[int]] = field(default_factory=dict)
    linked: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def to_link(self) -> int:
        return sum(len(ids) for ids in self.missing.values())


def link_attendance_devices(
    db: Session, tenant: Tenant, odoo: OdooClient, apply: bool = False
) -> LinkReport:
    """Report — and with ``apply``, fix — BioBridge-created hr.attendance
    records that have no device in Odoo. The caller commits (or rolls back,
    for a report-only run) the punch links ``device_for_punch`` fills in."""
    report = LinkReport()

    punches = db.scalars(
        select(PunchRecord)
        .where(
            PunchRecord.tenant_id == tenant.id,
            PunchRecord.odoo_attendance_id.is_not(None),
        )
        .order_by(PunchRecord.punch_time_utc)
    ).all()

    all_ids: set[int] = set()
    device_of: dict[int, Device] = {}
    for punch in punches:
        attendance_id = punch.odoo_attendance_id
        all_ids.add(attendance_id)
        if attendance_id in device_of:
            continue  # earliest traceable punch wins — the check-in, normally
        device = device_for_punch(db, punch)
        if device is not None:
            device_of[attendance_id] = device

    report.attendances = len(all_ids)
    report.no_terminal = len(all_ids) - len(device_of)
    if not device_of:
        return report

    without = set(odoo.attendance_ids_without_device(sorted(device_of)))
    report.already_linked = len(device_of) - len(without)

    by_device: dict[str, list[int]] = defaultdict(list)
    devices: dict[str, Device] = {}
    for attendance_id in sorted(without):
        device = device_of[attendance_id]
        by_device[device.id].append(attendance_id)
        devices[device.id] = device
    report.missing = {devices[d].serial_number: ids for d, ids in by_device.items()}

    if not apply:
        return report

    for device_id, ids in by_device.items():
        device = devices[device_id]
        try:
            odoo_device_id = odoo.upsert_device(
                device.serial_number,
                name=device.alias,
                location=device.area,
                terminal_model=device.model,
                ip_address=device.ip_address,
            )
            odoo.set_attendance_device(ids, odoo_device_id)
        except OdooError as exc:
            # One terminal Odoo won't take must not stop the others.
            report.failures.append(f"{device.serial_number}: {exc}")
            continue
        report.linked += len(ids)
    return report
