"""Import the device's users that already match an Odoo employee.

The Employees page used to fill only from punches. Once Odoo and a biometric
connection are both up, this reads the device's user list and links every user
whose badge / PIN / registration number / work email matches exactly one active
Odoo employee, with or without a punch. It runs at the end of every sync and
straight after either connection is saved or tested.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.base import Capability, get_provider_class
from app.integrations.odoo import MATCH_FIELDS, OdooClient, OdooError
from app.models import DeviceSource, EmployeeMapping, MappingStatus, OdooConnection, Tenant
from app.services.connections import UnsafeTargetError, build_odoo_client, build_source_provider

log = logging.getLogger(__name__)


def import_matched_employees(
    db: Session,
    tenant: Tenant,
    odoo: OdooClient,
    sources: list[DeviceSource],
    say: Callable[..., None] | None = None,
    provider_factory: Callable[..., object] | None = None,
) -> int:
    """Returns how many device users were newly linked. Existing mapped,
    ignored and out-of-scope rows are never touched; ambiguous matches and
    device users with no Odoo counterpart are skipped."""
    say = say or (lambda message, level="info": log.info(message))
    provider_factory = provider_factory or build_source_provider

    device_users: dict[str, str] = {}
    for source in sources:
        try:
            # Don't open a connection to a vendor that can't list its users.
            if Capability.READ_EMPLOYEES not in get_provider_class(source.provider).capabilities:
                continue
        except Exception:  # noqa: BLE001 — unknown slug: let the factory decide below
            pass
        try:
            provider = provider_factory(tenant, source)
        except Exception:  # noqa: BLE001
            continue
        try:
            if not provider.supports(Capability.READ_EMPLOYEES):
                continue
            for rec in provider.fetch_employees():
                code = (rec.emp_code or "").strip()
                if code and code not in device_users:
                    device_users[code] = rec.full_name
        except Exception as exc:  # noqa: BLE001 — one source must never stop the import (or the sync)
            say(f"'{source.name}': could not read its users for import: {exc}", "warning")
        finally:
            try:
                provider.close()
            except Exception:  # noqa: BLE001
                pass
    if not device_users:
        return 0

    existing = {
        m.emp_code: m
        for m in db.scalars(
            select(EmployeeMapping).where(EmployeeMapping.tenant_id == tenant.id)
        ).all()
    }
    todo = {
        c: n for c, n in device_users.items()
        if c not in existing
        or existing[c].status in (MappingStatus.unmapped.value, MappingStatus.ambiguous.value)
    }
    if not todo:
        return 0

    try:
        roster = [r for r in odoo.list_employees() if r.get("active", True)]
    except OdooError as exc:
        say(f"Could not read the Odoo roster for import: {exc}", "warning")
        return 0

    index: dict[tuple[str, str], list[dict]] = {}
    for row in roster:
        for field_name, _method in MATCH_FIELDS:
            value = row.get(field_name)
            if value:
                index.setdefault((field_name, str(value).strip()), []).append(row)

    cap = tenant.plan_max_employees
    mapped_count = 0
    if cap is not None:
        mapped_count = db.scalar(
            select(func.count(EmployeeMapping.id)).where(
                EmployeeMapping.tenant_id == tenant.id,
                EmployeeMapping.status == MappingStatus.mapped.value,
            )
        ) or 0

    imported = 0
    for code, name in sorted(todo.items()):
        if cap is not None and mapped_count >= cap:
            break
        hit = None
        for field_name, method in MATCH_FIELDS:
            rows = index.get((field_name, code), [])
            if len(rows) == 1:
                hit = (rows[0], method)
                break
            if len(rows) > 1:
                break  # ambiguous: leave it for a human, as find_employee does
        if hit is None:
            continue
        row, method = hit
        mapping = existing.get(code)
        if mapping is None:
            mapping = EmployeeMapping(tenant_id=tenant.id, emp_code=code)
            db.add(mapping)
            existing[code] = mapping
        mapping.odoo_employee_id = row["id"]
        mapping.odoo_employee_name = row.get("name")
        apply_details(mapping, row)  # bare-id names are filled by refresh_companies
        mapping.source_name = name or mapping.source_name
        mapping.status = MappingStatus.mapped.value
        mapping.match_method = method
        mapping.match_note = None
        imported += 1
        mapped_count += 1

    if imported:
        db.flush()
        say(f"Imported {imported} device user(s) already matched to Odoo employees")
    return imported


def _m2o(row: dict, field: str, names: dict[int, str] | None = None) -> tuple[int | None, str | None]:
    """A many2one on an ``hr.employee`` row, whichever way this Odoo API
    spells it: ``[id, name]``, ``{"id":…, "display_name":…}`` or a bare id
    (then the name comes from ``names``)."""
    value = row.get(field)
    if isinstance(value, (list, tuple)) and value:
        return value[0], (value[1] if len(value) > 1 else (names or {}).get(value[0]))
    if isinstance(value, dict) and value.get("id"):
        return value["id"], value.get("display_name") or value.get("name") or (names or {}).get(value["id"])
    if isinstance(value, int) and not isinstance(value, bool):
        return value, (names or {}).get(value)
    return None, None


def _company_of(row: dict, names: dict[int, str] | None = None) -> tuple[int | None, str | None]:
    return _m2o(row, "company_id", names)


def apply_details(m: EmployeeMapping, row: dict, companies=None, departments=None, people=None) -> bool:
    """Copy company, department and manager from an Odoo roster row onto a
    mapping. Returns True when anything changed."""
    cid, cname = _m2o(row, "company_id", companies)
    _, dept = _m2o(row, "department_id", departments)
    mid, mname = _m2o(row, "parent_id", people)
    new = (cid, cname, dept, mid, mname)
    old = (m.odoo_company_id, m.odoo_company_name, m.odoo_department_name,
           m.odoo_manager_id, m.odoo_manager_name)
    if new == old:
        return False
    (m.odoo_company_id, m.odoo_company_name, m.odoo_department_name,
     m.odoo_manager_id, m.odoo_manager_name) = new
    return True


def _lookup_names(odoo: OdooClient, model: str) -> dict[int, str]:
    try:
        if model == "res.company":
            return {c["id"]: c.get("name") for c in odoo.list_companies()}
        rows = odoo.execute(model, "search_read", [[]], {"fields": ["id", "name"]}) or []
        return {r["id"]: r.get("name") for r in rows}
    except Exception:  # noqa: BLE001 — names are a nicety; the roster usually carries them
        return {}


def refresh_companies(db: Session, tenant: Tenant, odoo: OdooClient, force: bool = False) -> int:
    """Fill each mapped employee's Odoo company, department and manager (for
    the Employees page's filters). Costs one roster read; without ``force`` it
    only runs when some mapped row has no company recorded yet. Returns how
    many rows changed."""
    rows = db.scalars(
        select(EmployeeMapping).where(
            EmployeeMapping.tenant_id == tenant.id,
            EmployeeMapping.status == MappingStatus.mapped.value,
            EmployeeMapping.odoo_employee_id.is_not(None),
        )
    ).all()
    if not force and not any(m.odoo_company_id is None for m in rows):
        return 0
    try:
        roster_rows = odoo.list_employees()
    except OdooError as exc:
        log.warning("could not read Odoo employee details: %s", exc)
        return 0
    roster = {r["id"]: r for r in roster_rows}
    people = {r["id"]: r.get("name") for r in roster_rows}
    needs = lambda f: any(isinstance(r.get(f), int) and not isinstance(r.get(f), bool) for r in roster_rows)  # noqa: E731
    companies = _lookup_names(odoo, "res.company") if needs("company_id") else {}
    departments = _lookup_names(odoo, "hr.department") if needs("department_id") else {}
    changed = 0
    for m in rows:
        row = roster.get(m.odoo_employee_id)
        if row is not None and apply_details(m, row, companies, departments, people):
            changed += 1
    if changed:
        db.flush()
    return changed


def auto_import(db: Session, tenant: Tenant) -> int:
    """Best-effort import when a connection is saved/tested or a device sends
    its user list. Does nothing unless both an Odoo connection and a
    biometric connection exist and work; never raises."""
    try:
        if not tenant.syncable:
            return 0
        conn = db.scalars(
            select(OdooConnection).where(
                OdooConnection.tenant_id == tenant.id, OdooConnection.is_active.is_(True)
            ).limit(1)
        ).first()
        sources = list(db.scalars(
            select(DeviceSource).where(
                DeviceSource.tenant_id == tenant.id, DeviceSource.is_active.is_(True)
            )
        ).all())
        if conn is None or not sources:
            return 0
        odoo = build_odoo_client(tenant, conn)
        odoo.authenticate()
        imported = import_matched_employees(db, tenant, odoo, sources)
        refresh_companies(db, tenant, odoo)
        return imported
    except Exception:  # noqa: BLE001 — a convenience, never a reason to fail a save
        log.warning("auto roster import skipped", exc_info=True)
        return 0
