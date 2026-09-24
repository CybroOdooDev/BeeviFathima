"""Create Odoo employees on a device, as part of "Import terminals".

The rule, as the product owner set it: on import, an **active** Odoo
employee who is **not mapped to a device user yet** and **has a Badge ID
(``barcode``) or a PIN** is created on the device, with that value as their
device user id. Nothing else qualifies — not a registration number or a work
email, which the broader matching order (``MATCH_FIELDS``) and the sync-time
``auto_provision_employees`` also fall back to. A device user id has to be
short enough and, on some ZKTeco firmware, numeric; a badge or PIN is what
people actually punch with, an email never is.

"Not mapped" means both of these:

* no ``EmployeeMapping`` in BioBridge already ties this Odoo employee to a
  badge (someone matched by hand to a different code must not get a second
  device user under their Odoo code), and
* the device has no user with that code already (so re-running Import
  terminals never creates duplicates).

Identity only, as everywhere else: the device gets a user id and name. A
fingerprint, face or card still has to be enrolled at the terminal.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app.integrations.base import AttendanceProvider, EmployeeRecord, ProviderError, UnsupportedCapability

#: Only these, in this order — see the module docstring.
PROVISION_FIELDS: tuple[str, ...] = ("barcode", "pin")


@dataclass
class ProvisionResult:
    created: list[dict] = field(default_factory=list)       # {emp_code, name}
    failed: list[dict] = field(default_factory=list)        # {emp_code, name, error}
    already_on_device: int = 0
    already_mapped: int = 0
    no_badge_or_pin: int = 0


def provision_code(row: dict) -> str | None:
    """The Badge ID, else the PIN, of an ``hr.employee`` row; None if neither.
    Odoo returns ``False`` for an empty field, which reads as falsy here."""
    for name in PROVISION_FIELDS:
        value = row.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return None


def provision_unmapped(
    provider: AttendanceProvider,
    roster: list[dict],
    mapped_odoo_ids: set[int],
) -> ProvisionResult:
    """Create every qualifying Odoo employee on ``provider``'s device.

    ``roster`` is ``OdooClient.list_employees()``; ``mapped_odoo_ids`` the
    Odoo ids BioBridge already has a mapped badge for. Reads the device's
    user list once up front. One employee failing (a code this device's
    layout can't hold, say) never stops the rest; each failure is reported
    with its reason. A device that can't be read at all raises, since
    nothing can be decided without its user list.
    """
    result = ProvisionResult()
    candidates: list[tuple[str, str]] = []  # (code, name)
    for row in roster:
        if not row.get("active", True):
            continue
        if row.get("id") in mapped_odoo_ids:
            result.already_mapped += 1
            continue
        code = provision_code(row)
        if code is None:
            result.no_badge_or_pin += 1
            continue
        candidates.append((code, row.get("name") or code))

    # Two Odoo employees with the same badge would become one device user —
    # whose punches then match neither of them (find_employee reports the
    # badge as ambiguous). Refuse both and say why, rather than picking one.
    shared = {code for code, n in Counter(c for c, _ in candidates).items() if n > 1}

    on_device = {e.emp_code for e in provider.fetch_employees() if e.emp_code}

    for code, name in candidates:
        if code in shared:
            result.failed.append({
                "emp_code": code, "name": name,
                "error": "Several Odoo employees share this Badge ID/PIN — give each their own.",
            })
            continue
        if code in on_device:
            result.already_on_device += 1
            continue
        first_name, _, last_name = name.partition(" ")
        try:
            provider.create_employee(
                EmployeeRecord(
                    external_id=None, emp_code=code,
                    first_name=first_name, last_name=last_name,
                )
            )
        except (ProviderError, UnsupportedCapability) as exc:
            result.failed.append({"emp_code": code, "name": name, "error": str(exc)})
            continue
        on_device.add(code)
        result.created.append({"emp_code": code, "name": name})
    return result
