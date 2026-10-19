#!/usr/bin/env python3
"""Create a few sample employees in one company of a real, multi-company Odoo.

Written for testing ``OdooConnection.company_id`` against a real instance —
run it once for company 2 and once for company 1 (or reuse whatever roster
company 1 already has) and you have exactly the scenario the isolation
tests fake: two companies, two rosters, and a way to confirm a connection
scoped to one of them can never see or touch the other's.

Talks straight to ``OdooClient`` — no BioBridge tenant or database involved,
same as ``e2e_proof.py`` and ``schedule_proof.py``, just without the sync
engine in between since there's no punch stream here to drive.

    python3 tools/create_company_employees.py --odoo-url https://acme.odoo.com \
        --odoo-db acme --odoo-user bot@acme.com --odoo-key <api-key> \
        --company-id 2

Idempotent: re-running it finds each employee by the same emp_code (scoped
to --company-id, exactly like a real sync would) and reports "already
there" instead of creating a duplicate.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.integrations.odoo import OdooClient, OdooCredentials, OdooError  # noqa: E402

#: Styled after tools/mock_biotime.py's own EMPLOYEES fixture (Ahmed Sharma /
#: Sara Tanaka / Jane Haddad) — same shape, different people, so a roster
#: built by this script is never mistaken for that one's.
SAMPLE_EMPLOYEES = [
    {"name": "Liam Okafor", "emp_code": "2001"},
    {"name": "Priya Nakamura", "emp_code": "2002"},
    {"name": "Noah Fernandes", "emp_code": "2003"},
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--odoo-url", required=True)
    parser.add_argument("--odoo-db", required=True)
    parser.add_argument("--odoo-user", required=True)
    parser.add_argument("--odoo-key", required=True)
    parser.add_argument(
        "--company-id", type=int, default=None,
        help="res.company id to create these employees in — required unless "
        "--list-companies is given. Test Connection in the BioBridge "
        "dashboard (or --list-companies here) shows the ids a given login "
        "can see.",
    )
    parser.add_argument(
        "--list-companies", action="store_true",
        help="Print every company this login can see and exit — nothing is created.",
    )
    args = parser.parse_args()
    if args.company_id is None and not args.list_companies:
        parser.error("--company-id is required unless --list-companies is given")

    client = OdooClient(
        OdooCredentials(
            url=args.odoo_url, db=args.odoo_db, username=args.odoo_user, api_key=args.odoo_key,
            company_id=None if args.list_companies else args.company_id,
        )
    )

    try:
        client.authenticate()
        companies = client.list_companies()
    except OdooError as exc:
        print(f"Could not reach Odoo: {exc}")
        raise SystemExit(1) from None

    if args.list_companies:
        print("Companies visible to this login:")
        for c in companies:
            print(f"  {c['id']:>4}  {c['name']}")
        return

    match = next((c for c in companies if c["id"] == args.company_id), None)
    if match is None:
        print(f"This login cannot see company id {args.company_id}. Visible companies:")
        for c in companies:
            print(f"  {c['id']:>4}  {c['name']}")
        raise SystemExit(1)

    print(f"Company {args.company_id} — {match['name']}\n")

    for row in SAMPLE_EMPLOYEES:
        emp_id, name, method = client.find_employee(row["emp_code"])
        if emp_id is not None:
            print(f"  already there   {row['emp_code']:<6} {name}  (matched on {method}, id {emp_id})")
            continue
        try:
            new_id = client.create_employee(row["name"], row["emp_code"])
        except OdooError as exc:
            print(f"  FAILED          {row['emp_code']:<6} {row['name']}  -- {exc}")
            continue
        print(f"  created         {row['emp_code']:<6} {row['name']}  (id {new_id})")

    print(
        f"\nDone. These now exist only in company {args.company_id} — a "
        "BioBridge OdooConnection with a *different* company_id (or none, "
        "against a single-company Odoo) will not find or list them."
    )


if __name__ == "__main__":
    main()
