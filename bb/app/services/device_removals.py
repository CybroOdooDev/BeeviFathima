"""Odoo device records waiting to be deleted.

When a connection is removed, its terminals' Odoo device records go too —
unless another connection of the tenant still has the same terminal (same
serial number, or same IP address). Each one is queued as an
``OdooDeviceRemoval`` first and deleted from the queue only once Odoo has
confirmed, so an Odoo that is down (or refuses) at removal time is caught up
by the next sync that reaches it.
"""
from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.integrations.odoo import OdooError
from app.models import Device, DeviceSource, OdooDeviceRemoval
from app.services.company_scope import terminal_company_id

log = logging.getLogger(__name__)


def terminal_in_use(
    db: Session, tenant_id: str, serial: str, ip: str | None, exclude_source_id: str | None = None
) -> bool:
    """Whether any connection of the tenant (but the excluded one) has this
    terminal, by serial number or IP address."""
    same = [Device.serial_number == serial]
    if ip:
        same.append(Device.ip_address == ip)
    query = select(Device.id).where(Device.tenant_id == tenant_id, or_(*same))
    if exclude_source_id is not None:
        query = query.where(Device.source_id != exclude_source_id)
    return db.scalar(query.limit(1)) is not None


def queue_source_terminals(db: Session, tenant_id: str, source: DeviceSource) -> int:
    """Queue the Odoo removal of every terminal of ``source`` that no other
    connection has. Call before the source (and its terminals) is deleted."""
    queued = 0
    for device in db.scalars(select(Device).where(Device.source_id == source.id)).all():
        if terminal_in_use(db, tenant_id, device.serial_number, device.ip_address, source.id):
            continue
        already = db.scalar(select(OdooDeviceRemoval.id).where(
            OdooDeviceRemoval.tenant_id == tenant_id,
            OdooDeviceRemoval.serial_number == device.serial_number,
        ).limit(1))
        if already is not None:
            continue
        db.add(OdooDeviceRemoval(
            tenant_id=tenant_id,
            serial_number=device.serial_number,
            ip_address=device.ip_address,
            company_id=terminal_company_id(db, device),
            source_name=source.name,
        ))
        queued += 1
    db.flush()
    return queued


def work_off(db: Session, tenant_id: str, odoo) -> tuple[int, list[str]]:
    """Delete every queued device from Odoo. Returns (records deleted, error
    lines). A terminal that is back in BioBridge meanwhile is dropped from the
    queue untouched; one Odoo no longer has counts as done. Never raises."""
    removed, errors = 0, []
    for row in db.scalars(
        select(OdooDeviceRemoval).where(OdooDeviceRemoval.tenant_id == tenant_id)
        .order_by(OdooDeviceRemoval.created_at)
    ).all():
        if terminal_in_use(db, tenant_id, row.serial_number, row.ip_address):
            db.delete(row)
            continue
        try:
            if odoo.delete_device(row.serial_number, company_id=row.company_id):
                removed += 1
        except OdooError as exc:
            row.attempts += 1
            row.last_error = str(exc)[:500]
            errors.append(f"{row.serial_number}: {exc}")
            log.warning("Could not remove device %s from Odoo: %s", row.serial_number, exc)
            continue
        db.delete(row)
    db.flush()
    return removed, errors
