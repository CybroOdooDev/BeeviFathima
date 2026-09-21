#!/usr/bin/env python3
"""Grant or revoke platform staff access.

Platform staff can see and change **every** customer's sync cadence. That
crosses the isolation boundary the rest of the system is built on, so no HTTP
route sets this flag — not signup, not any user screen, not another admin. The
only way to grant it is here, on the server, with database access.

That is the point: the privilege costs exactly what a database login costs, and
it cannot be escalated through the product. Someone who compromises a support
account cannot promote themselves or anyone else.

    python3 tools/grant_admin.py --list
    python3 tools/grant_admin.py --create --email ops@yourcompany.com
    python3 tools/grant_admin.py --email someone@existing.com
    python3 tools/grant_admin.py --email someone@existing.com --revoke

``--create`` makes a staff user with **no tenant**, which is the right shape: a
support engineer is not a customer. Forcing a tenant on them puts a phantom
company in the customer list, counts it in "N accounts scheduled", and polls a
BioTime server that does not exist — while handing the engineer a meaningless
attendance dashboard of their own.

Promoting an *existing* user is still allowed and keeps their tenant, for the
case where someone genuinely is both a customer and staff. The listing says
which is which.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.models import Tenant, User, UserRole  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", help="The user to grant, revoke, or create.")
    parser.add_argument("--create", action="store_true",
                        help="Create a new staff user with no tenant. Needs --email.")
    parser.add_argument("--password", help="For --create. Generated if omitted.")
    parser.add_argument("--name", help="For --create. Optional display name.")
    parser.add_argument("--revoke", action="store_true", help="Take the access away.")
    parser.add_argument("--list", action="store_true", help="Show current platform staff.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation.")
    args = parser.parse_args()

    if not args.email and not args.list:
        parser.error("give --email, or --list")
    if args.create and not args.email:
        parser.error("--create needs --email")

    print(f"database: {settings.database_url}\n")

    with session_scope() as db:
        if args.list:
            staff = db.scalars(
                select(User).where(User.is_platform_admin.is_(True)).order_by(User.email)
            ).all()
            if not staff:
                print("No platform staff. Nobody can reach the cross-tenant console.")
                return 0
            print(f"{len(staff)} platform user(s):")
            for user in staff:
                # Guarded: db.get with a null key warns, and a tenantless staff
                # user is now the normal case rather than the odd one.
                tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
                state = "" if user.is_active else "  [DISABLED]"
                where = (
                    "no tenant — platform staff only"
                    if user.tenant_id is None
                    else f"ALSO a customer: {tenant.name if tenant else '(missing)'}"
                )
                print(f"  {user.email:<40} {where}{state}")
            if not args.email:
                return 0

        email = args.email.lower()
        user = db.scalars(select(User).where(User.email == email)).first()

        # --- create a tenantless staff user ---------------------------------
        if args.create:
            if user is not None:
                print(f"{email} already exists"
                      + (" and is already staff." if user.is_platform_admin
                         else ". Promote them by dropping --create."), file=sys.stderr)
                return 1
            password = args.password or secrets.token_urlsafe(16)
            db.add(
                User(
                    tenant_id=None,  # the point: staff are not a customer
                    email=email,
                    full_name=args.name,
                    hashed_password=hash_password(password),
                    role=UserRole.owner.value,  # unused without a tenant
                    is_platform_admin=True,
                )
            )
            db.commit()
            print(f"Created platform user {email} with no tenant.\n")
            print(f"  password: {password}")
            print("\nShown once — only the hash is stored.")
            print("\nThey sign in at the console door, not the customer one:")
            print("    <dashboard URL>/#/staff/login")
            print("The customer login will turn them away: they have no workspace "
                  "of their\nown, which is deliberate. Console sessions are also "
                  "short-lived, so\nexpect to sign in again more often than on the "
                  "customer side.")
            return 0

        if user is None:
            print(f"No user with email {args.email}.", file=sys.stderr)
            print("Use --create to make a staff user with no tenant, or sign them "
                  "up in the\ndashboard first if they should also be a customer.",
                  file=sys.stderr)
            return 1

        target = not args.revoke
        if user.is_platform_admin == target:
            print(f"{user.email} already has is_platform_admin={target}. Nothing to do.")
            return 0

        tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
        verb = "REVOKE platform access from" if args.revoke else "GRANT platform access to"
        where = f"own account: {tenant.name}" if tenant else "no tenant"
        print(f"{verb} {user.email} ({where})")
        if not args.revoke:
            print("  They will be able to see and change the sync cadence of every "
                  "customer,\n  and their changes are written into each customer's "
                  "own audit trail.")
        if not args.yes:
            if input("Type 'yes' to continue: ").strip().lower() != "yes":
                print("aborted")
                return 1

        user.is_platform_admin = target
        db.commit()
        print(f"\n{user.email}: is_platform_admin = {target}")
        if target:
            # The flag is necessary and not sufficient: the console also requires
            # a session minted at its own door, so "sign in again" is not enough
            # if they sign in at the same place as before.
            print("\nThe flag alone does not open the console. They must sign in at")
            print("    <dashboard URL>/#/staff/login")
            print("Any session they are holding now is a customer one and the "
                  "console will\nrefuse it. If they also have their own workspace, "
                  "the sidebar there now\nlinks across to the console door.")
        else:
            print("\nTheir console sessions stop working immediately, and any "
                  "refresh of one\nis refused.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
