#!/usr/bin/env python3
"""Show why Odoo refuses access to a BioBridge Device — read-only.

"doesn't have 'read' access to BioBridge Device … Blame the following rules:
x_biobridge_device.biobridge_company" means a record rule hid a device from the
companies active in that request. Which of several causes it is depends on
things only the customer's Odoo knows: the rule's actual domain, which company
each device row carries, which companies the Odoo user belongs to, and which
company BioBridge's connection pins every call to. This prints all four side by
side, then says which combination is the problem.

    python3 tools/check_device_access.py
    python3 tools/check_device_access.py --tenant acme

Nothing is written, in BioBridge or in Odoo. Reads BioBridge's database directly
for each account's stored Odoo credentials, like check_source.py. Run it from
the repo root.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.integrations.odoo import OdooError  # noqa: E402
from app.models import Device, OdooConnection, Tenant  # noqa: E402
from app.services.connections import UnsafeTargetError, build_odoo_client  # noqa: E402

MODEL = "x_biobridge_device"
RULE_NAME = f"{MODEL}.biobridge_company"
CURRENT_DOMAIN = "['|', ('x_company_id', '=', False), ('x_company_id', 'in', company_ids)]"


def _m2o(value):
    """(id, name) for a many2one read, or (None, '—') when unset."""
    if isinstance(value, (list, tuple)) and value:
        return value[0], value[1]
    return None, "—"


def check(db, tenant) -> int:
    problems: list[str] = []
    conn = db.scalars(
        select(OdooConnection).where(
            OdooConnection.tenant_id == tenant.id, OdooConnection.is_active.is_(True)
        )
    ).all()
    print(f"\n=== {tenant.slug}")
    if not conn:
        print("  no active Odoo connection")
        return 0
    if len(conn) > 1:
        problems.append(
            f"{len(conn)} active Odoo connections on this account — BioBridge uses "
            "whichever it finds first, so the company it registers devices under "
            "can change between runs. Keep one active connection per account."
        )
    conn = conn[0]
    print(f"  Odoo connection : {conn.name} → {conn.url} (db {conn.db_name})")
    print(f"  pinned company  : {conn.company_id if conn.company_id is not None else 'none (every company the user can see)'}"
          f"{f' — {conn.company_name}' if conn.company_name else ''}")
    print(f"  device tracking : {conn.device_tracking_mode or 'off'}")

    try:
        odoo = build_odoo_client(tenant, conn)
        uid = odoo.authenticate()
    except (OdooError, UnsafeTargetError) as exc:
        print(f"  could not reach Odoo: {exc}")
        return 1

    def call(model, method, args, kwargs=None, companies=None):
        kwargs = dict(kwargs or {})
        ctx = {"active_test": False}
        if companies:
            ctx["allowed_company_ids"] = companies
        kwargs["context"] = ctx
        return odoo.execute(model, method, args, kwargs, scope_to_company=False)

    # -- the Odoo user ------------------------------------------------------
    user = call("res.users", "read", [[uid]], {"fields": ["name", "company_id", "company_ids"]})[0]
    user_companies = list(user["company_ids"])
    print(f"  Odoo user       : {user['name']} (id={uid}), allowed companies {user_companies}, "
          f"default {_m2o(user['company_id'])[0]}")
    if conn.company_id is not None and conn.company_id not in user_companies:
        problems.append(
            f"The connection is pinned to company {conn.company_id}, but {user['name']} "
            f"isn't allowed in it (allowed: {user_companies}). Add that company to the "
            "user's Allowed Companies in Odoo, or pin the connection to one they have."
        )

    if not call("ir.model", "search", [[("model", "=", MODEL)]]):
        print(f"  {MODEL} doesn't exist in this Odoo — device tracking was never set up here")
        return 0

    # -- record rules on the device model ------------------------------------
    rules = call(
        "ir.rule", "search_read", [[("model_id.model", "=", MODEL)]],
        {"fields": ["name", "domain_force", "active", "groups",
                    "perm_read", "perm_write", "perm_create", "perm_unlink"]},
    )
    print(f"  record rules on {MODEL}: {len(rules)}")
    ours = None
    for r in rules:
        state = "" if r["active"] else "  (archived)"
        scope = "global" if not r["groups"] else f"groups {r['groups']}"
        print(f"    - {r['name']}{state} [{scope}]")
        print(f"        {r['domain_force']}")
        if r["name"] == RULE_NAME:
            ours = r
        elif r["active"]:
            problems.append(
                f"Extra rule {r['name']!r} on {MODEL} — BioBridge doesn't create it and "
                "doesn't know what it requires. If it isn't needed, archive it."
            )
    if ours is None:
        problems.append(f"BioBridge's own rule {RULE_NAME!r} is missing — run Update setup.")
    elif " ".join(ours["domain_force"].split()) != CURRENT_DOMAIN:
        problems.append(
            f"{RULE_NAME} still has an old domain ({ours['domain_force']}). "
            "Update setup with the latest odoo.py rewrites it — or edit it by hand to "
            f"{CURRENT_DOMAIN}. If Update setup already ran, BioBridge wasn't "
            "restarted onto the new code, or the connection's user can't write ir.rule."
        )

    # -- the device rows, as seen with every company the user has ------------
    devices = call(
        MODEL, "search_read", [[]],
        {"fields": ["x_name", "x_serial_number", "x_company_id"], "order": "id"},
        companies=user_companies,
    )
    print(f"  devices in Odoo (all of {user['name']}'s companies active): {len(devices)}")
    for d in devices:
        cid, cname = _m2o(d.get("x_company_id"))
        flag = ""
        if conn.company_id is not None and cid is not None and cid != conn.company_id:
            flag = f"   ← belongs to company {cid}; hidden from this connection (pinned to {conn.company_id})"
        print(f"    #{d['id']:<4} {d.get('x_serial_number') or '?':<16} {d.get('x_name') or '':<20} "
              f"company: {cname}{flag}")

    # -- this account's terminals vs Odoo's ----------------------------------
    serials = {d.get("x_serial_number"): d for d in devices}
    local = db.scalars(select(Device).where(Device.tenant_id == tenant.id)).all()
    for dev in local:
        row = serials.get(dev.serial_number)
        if row is None:
            continue
        cid, _ = _m2o(row.get("x_company_id"))
        if conn.company_id is not None and cid is not None and cid != conn.company_id:
            problems.append(
                f"{dev.serial_number} is one of this account's terminals, but its Odoo "
                f"device (#{row['id']}) belongs to company {cid} while the connection is "
                f"pinned to {conn.company_id}. Any attendance or device update BioBridge "
                "makes for it hits the rule. One BioBridge account serves one company: "
                "move that source to the account for its company, or fix the device's "
                "Company in Odoo."
            )
    if conn.company_id is None:
        problems.append(
            "The connection isn't pinned to a company, so new devices get no company and "
            "are visible in every company. Pin it (Settings → Odoo → company) if this "
            "account belongs to one company."
        )

    print("  diagnosis:")
    if not problems:
        print("    nothing wrong found here — if the error is in Odoo's own screens, the "
              "company switcher (top right) probably has only a company that the device "
              "shown in the error doesn't belong to. That is the rule working; tick that "
              "company too to see it.")
    for p in problems:
        print(f"    • {p}")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tenant", help="Only this account (its slug).")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        query = select(Tenant).order_by(Tenant.slug)
        if args.tenant:
            query = query.where(Tenant.slug == args.tenant)
        tenants = db.scalars(query).all()
        if not tenants:
            print(f"No account with slug {args.tenant!r}." if args.tenant else "No accounts.")
            return 1
        worst = 0
        for tenant in tenants:
            try:
                worst = max(worst, check(db, tenant))
            except OdooError as exc:
                print(f"  Odoo refused a read: {exc}")
                worst = 1
        return worst
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
