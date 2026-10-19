#!/usr/bin/env python3
"""Inspect user accounts, and say why a login is failing.

Reads the database directly rather than through the API, on purpose: the usual
reason someone runs this is that the API will not let them in, and a diagnostic
that needs the thing you are diagnosing is no diagnostic.

    python3 tools/show_users.py                      # every account
    python3 tools/show_users.py --email you@acme.com # one, in detail
    python3 tools/show_users.py --check you@acme.com --password 'secret'

    python3 tools/show_users.py --email you@acme.com --unlock
    python3 tools/show_users.py --email you@acme.com --set-password 'new one'

``--check`` verifies a password against the stored hash without going near the
HTTP layer, which separates "the password is wrong" from "something between the
browser and the database is broken" — two problems that look identical from a
login form.

Password hashes are never printed. ``--set-password`` exists because the only
other way back into a locked-out account is editing the database by hand, and a
support person doing that with a text editor is worse than a tool that does it
correctly.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password, verify_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.schema_check import detect_drift  # noqa: E402
from app.db.session import engine, session_scope  # noqa: E402
from app.models import Tenant, User  # noqa: E402
import app.models  # noqa: E402,F401


class Row:
    """A user read as plain columns, so a stale schema cannot break the read.

    ``select(User)`` names every column the *model* declares. On a database that
    is a migration behind, that is the very query which fails — so the tool
    would die from the error it exists to explain. This reads only the columns
    the database actually has, and reports the rest as absent.
    """

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, name):
        return self._data.get(name)


def read_users_raw(db, email: str | None) -> list[Row]:
    live = [c["name"] for c in inspect(engine).get_columns("app_user")]
    columns = ", ".join(live)
    sql = f"SELECT {columns} FROM app_user"  # noqa: S608 — names from the schema
    params = {}
    if email:
        sql += " WHERE email = :email"
        params["email"] = email.lower()
    sql += " ORDER BY email"
    return [Row(dict(zip(live, row))) for row in db.execute(text(sql), params).all()]


def _aware(value):
    if value is None:
        return None
    if isinstance(value, str):  # SQLite hands back strings on a raw read
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def login_blockers(user: User) -> list[str]:
    """Every reason the login endpoint would refuse this account, in its order."""
    reasons = []
    locked = _aware(user.locked_until)
    if locked and locked > datetime.now(timezone.utc):
        wait = int((locked - datetime.now(timezone.utc)).total_seconds() / 60) + 1
        reasons.append(
            f"LOCKED for another {wait} min after repeated failures "
            f"(429, not 401 — the error names the wait). Clear it with --unlock."
        )
    if not user.is_active:
        reasons.append("DISABLED (is_active is false) — login answers 403.")
    return reasons


def check_schema() -> bool:
    """A database behind the models breaks login before any password is checked.

    ``select(User)`` names every column the model declares, so one missing column
    raises ``no such column`` and the endpoint returns 500 — which from a login
    form is indistinguishable from a wrong password.
    """
    drift = detect_drift(engine, Base.metadata)
    if drift.is_empty:
        return True
    print("!! The database is behind the models — missing " + drift.summary())
    if any(t == "app_user" for t, _ in drift.missing_columns):
        print("!! app_user is affected, so EVERY login returns HTTP 500 before")
        print("!! the password is even looked at. This is almost certainly your problem.")
    print("!! Fix:  python3 tools/migrate.py --apply\n")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", help="Show one account in detail, or act on it.")
    parser.add_argument("--check", metavar="EMAIL",
                        help="Verify a password against the stored hash.")
    parser.add_argument("--password", help="The password for --check or --set-password.")
    parser.add_argument("--unlock", action="store_true",
                        help="Clear the lockout and the failure counter.")
    parser.add_argument("--activate", action="store_true", help="Set is_active true.")
    parser.add_argument("--set-password", metavar="PASSWORD",
                        help="Replace the password. Needs --email.")
    args = parser.parse_args()

    print(f"database: {settings.database_url}\n")
    schema_ok = check_schema()

    with session_scope() as db:
        # Columns by name, so a stale schema does not take the whole tool down
        # with the very error it is meant to explain.
        if not schema_ok:
            print("Reading what the database does have, column by column.\n")

        if args.check:
            found = read_users_raw(db, args.check)
            user = found[0] if found else None
            if user is None:
                print(f"No account with email {args.check}")
                print("Emails are stored lowercase; signup lowercases them, login too.")
                return 1
            if not args.password:
                print("--check needs --password")
                return 2
            ok = verify_password(args.password, user.hashed_password)
            print(f"password matches the stored hash: {ok}")
            if ok:
                blockers = login_blockers(user)
                if blockers:
                    print("\nBut login would still be refused:")
                    for reason in blockers:
                        print(f"  - {reason}")
                else:
                    print("\nNothing on this account blocks login. If the form still "
                          "fails, the problem is between the browser and here — check "
                          "the API's own log for a 500.")
            else:
                print("\nThat is a genuine password mismatch, not a lockout or a "
                      "schema problem.")
                print("Recover with:  --email {} --set-password '...'".format(user.email))
            return 0

        users = read_users_raw(db, args.email)

        if not users:
            if args.email:
                print(f"No account with email {args.email}")
                print("Emails are stored lowercase. Try --email without a filter to "
                      "list them all.")
            else:
                print("No users at all. Nobody has signed up on this database.")
                print("If you expected accounts here, DATABASE_URL may point somewhere "
                      "else than the server you signed up against.")
            return 1

        # --- actions --------------------------------------------------------
        acted = False
        if args.unlock or args.activate or args.set_password:
            if not args.email:
                print("Changing an account needs --email, so it cannot hit everyone.")
                return 2
            # Writes go through raw SQL too, for the same reason reads do.
            email = args.email.lower()
            sets, params = [], {"email": email}
            if args.unlock:
                sets += ["locked_until = NULL", "failed_login_count = 0"]
            if args.activate:
                sets.append("is_active = 1")
            if args.set_password:
                sets += ["hashed_password = :pw", "locked_until = NULL",
                         "failed_login_count = 0"]
                params["pw"] = hash_password(args.set_password)
            db.execute(
                text(f"UPDATE app_user SET {', '.join(sets)} WHERE email = :email"),  # noqa: S608
                params,
            )
            db.commit()
            if args.unlock:
                print(f"unlocked {email}")
            if args.activate:
                print(f"activated {email}")
            if args.set_password:
                print(f"password replaced for {email} (and the lockout cleared)")
            acted = True
            users = read_users_raw(db, args.email)

        # --- report ---------------------------------------------------------
        print(f"\n{len(users)} account(s):\n")
        for user in users:
            tenant = db.get(Tenant, user.tenant_id)
            flags = []
            if not user.is_active:
                flags.append("DISABLED")
            if getattr(user, "is_platform_admin", False):
                flags.append("platform staff")
            locked = _aware(user.locked_until)
            if locked and locked > datetime.now(timezone.utc):
                flags.append("LOCKED")

            print(f"  {user.email}")
            print(f"      account    {tenant.name if tenant else '(orphaned)'} "
                  f"[{tenant.slug if tenant else '?'}]")
            print(f"      role       {user.role}"
                  + (f"   {', '.join(flags)}" if flags else ""))
            print(f"      last login {user.last_login_at or 'never'}")
            if user.failed_login_count:
                print(f"      failures   {user.failed_login_count} "
                      f"(locks at {settings.max_failed_logins})")
            if locked:
                print(f"      locked to  {locked}")
            for reason in login_blockers(user):
                print(f"      !! {reason}")
            print()

        if not acted and not args.email:
            print("Tip: --check <email> --password '...' tells you whether a password "
                  "is right,\n     without involving the API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
