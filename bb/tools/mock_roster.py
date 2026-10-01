"""Rosters for the mock BioTime tools, read from your real Odoo by company id.

Shared by tools/generate_punches.py and tools/mock_biotime.py so both always
describe the same people: pass ``--company <res.company id>`` to either and
they use that company's active employees from Odoo, each with their Badge ID
(else PIN, else registration number) as the device user id, and two mock
terminals of its own (``MOCK-C<id>-GATE-01`` / ``-02``).

Where the Odoo login comes from, first match wins:

1. ``--odoo-url --odoo-db --odoo-user --odoo-key`` on the command line;
2. ``ODOO_URL`` / ``ODOO_DB`` / ``ODOO_USER`` / ``ODOO_KEY`` in the environment;
3. the Odoo connection already saved in BioBridge (``biobridge.db``) — so on a
   machine where BioBridge is connected to Odoo, no flags are needed at all.
   With several saved connections to different Odoo servers, pick one with
   ``--tenant <account slug>``.

The roster is cached in ``roster_company<id>.json`` the first time it is read,
so the mock server starts instantly and keeps serving the same people even
when Odoo is unreachable; ``--refresh-roster`` reads it again.

Without any Odoo login, ``--company 1`` / ``2`` / ``4`` still fall back to the
small built-in fixtures these tools always had, so tests and old commands keep
working.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Identifiers a person could actually punch with, in Odoo's matching order.
#: Work email is left out on purpose — no terminal uses an email as a user id.
BADGE_FIELDS = ("barcode", "pin", "registration_number")


@dataclass
class Roster:
    company_id: int
    company_name: str
    source: str                                   # "odoo", "cache" or "fixture"
    departments: list[dict] = field(default_factory=list)
    employees: list[dict] = field(default_factory=list)
    terminals: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # names with no badge/PIN

    @property
    def emp_codes(self) -> list[str]:
        return [e["emp_code"] for e in self.employees]

    @property
    def serials(self) -> list[str]:
        return [t["sn"] for t in self.terminals]

    def to_json(self) -> dict:
        return {"company_id": self.company_id, "company_name": self.company_name,
                "departments": self.departments, "employees": self.employees,
                "terminals": self.terminals, "skipped": self.skipped}


def add_odoo_arguments(parser) -> None:
    group = parser.add_argument_group("Odoo (where --company's roster is read from)")
    group.add_argument("--odoo-url", default=os.environ.get("ODOO_URL"))
    group.add_argument("--odoo-db", default=os.environ.get("ODOO_DB"))
    group.add_argument("--odoo-user", default=os.environ.get("ODOO_USER"))
    group.add_argument("--odoo-key", default=os.environ.get("ODOO_KEY"))
    group.add_argument("--tenant", default=None,
                       help="BioBridge account slug whose saved Odoo connection to use, "
                            "when there is more than one")
    group.add_argument("--refresh-roster", action="store_true",
                       help="Read the roster from Odoo again instead of the cached "
                            "roster_company<id>.json")
    group.add_argument("--list-companies", action="store_true",
                       help="Print the companies the Odoo login can see, and exit")


def roster_path(company_id: int, directory: Path | None = None) -> Path:
    return (directory or Path.cwd()) / f"roster_company{company_id}.json"


def terminals_for(company_id: int) -> list[dict]:
    base = 900 + company_id * 10
    return [
        {"id": base + 1, "sn": f"MOCK-C{company_id}-GATE-01", "alias": "Main Entrance",
         "ip_address": f"10.{company_id % 250}.0.11"},
        {"id": base + 2, "sn": f"MOCK-C{company_id}-GATE-02", "alias": "Back Door",
         "ip_address": f"10.{company_id % 250}.0.12"},
    ]


# --------------------------------------------------------------------------- #
# Odoo login
# --------------------------------------------------------------------------- #
def _saved_connection(tenant_slug: str | None):
    """(url, db, user, key) from BioBridge's saved Odoo connection, or None."""
    try:
        from app.core.crypto import decrypt
        from app.db.session import SessionLocal
        from app.models import OdooConnection, Tenant
    except Exception:  # noqa: BLE001 — not run from a BioBridge checkout
        return None
    try:
        with SessionLocal() as db:
            query = db.query(OdooConnection, Tenant).join(Tenant, Tenant.id == OdooConnection.tenant_id) \
                .filter(OdooConnection.is_active.is_(True))
            if tenant_slug:
                query = query.filter(Tenant.slug == tenant_slug)
            rows = query.all()
            if not rows:
                return None
            servers = {(c.url.rstrip("/"), c.db_name) for c, _ in rows}
            if len(servers) > 1 and not tenant_slug:
                options = ", ".join(sorted({t.slug for _, t in rows}))
                raise SystemExit("BioBridge has Odoo connections to more than one Odoo server — "
                                 f"pick one with --tenant ({options}), or pass --odoo-url/--odoo-db/"
                                 "--odoo-user/--odoo-key.")
            conn, tenant = rows[0]
            return conn.url, conn.db_name, conn.username, decrypt(conn.api_key_enc, tenant.crypto_key)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — no database here, or it is unreadable
        return None


def odoo_client(args, company_id: int | None):
    """An OdooClient scoped to ``company_id``, or None when no login is known."""
    creds = None
    if args.odoo_url and args.odoo_db and args.odoo_user and args.odoo_key:
        creds = (args.odoo_url, args.odoo_db, args.odoo_user, args.odoo_key)
    elif any((args.odoo_url, args.odoo_db, args.odoo_user, args.odoo_key)):
        raise SystemExit("Pass all four of --odoo-url, --odoo-db, --odoo-user and --odoo-key "
                         "(or the ODOO_* environment variables), or none of them.")
    else:
        creds = _saved_connection(getattr(args, "tenant", None))
    if creds is None:
        return None
    from app.integrations.odoo import OdooClient, OdooCredentials

    url, db, user, key = creds
    return OdooClient(OdooCredentials(url=url, db=db, username=user, api_key=key, company_id=company_id))


def list_companies(args) -> int:
    client = odoo_client(args, None)
    if client is None:
        raise SystemExit("No Odoo login found — pass --odoo-url/--odoo-db/--odoo-user/--odoo-key, "
                         "set ODOO_URL/ODOO_DB/ODOO_USER/ODOO_KEY, or connect Odoo in BioBridge first.")
    client.authenticate()
    print("Companies this Odoo login can see:")
    for company in client.list_companies():
        print(f"  {company['id']:>4}  {company['name']}")
    return 0


# --------------------------------------------------------------------------- #
# Rosters
# --------------------------------------------------------------------------- #
def _from_odoo(client, company_id: int) -> Roster:
    from app.integrations.odoo import OdooError

    try:
        client.authenticate()
        companies = {c["id"]: c["name"] for c in client.list_companies()}
    except OdooError as exc:
        raise SystemExit(f"Could not reach Odoo: {exc}") from None
    if company_id not in companies:
        listing = "\n".join(f"  {cid:>4}  {name}" for cid, name in sorted(companies.items()))
        raise SystemExit(f"This Odoo login can't see company id {company_id}. Companies it can see:\n{listing}")

    rows = [r for r in client.list_employees() if r.get("active", True)]
    departments: dict[int, dict] = {}
    employees, skipped, seen = [], [], set()
    for row in sorted(rows, key=lambda r: r["id"]):
        code = next((str(row[f]).strip() for f in BADGE_FIELDS if row.get(f)), None)
        if not code or code in seen:
            skipped.append(row.get("name") or f"employee {row['id']}")
            continue
        seen.add(code)
        dept = None
        if isinstance(row.get("department_id"), (list, tuple)) and row["department_id"]:
            dept_id, dept_name = row["department_id"][0], row["department_id"][1]
            dept = departments.setdefault(dept_id, {"id": dept_id, "dept_code": str(dept_id),
                                                    "dept_name": str(dept_name).split(" / ")[-1]})
        first, _, last = (row.get("name") or code).partition(" ")
        employees.append({"id": row["id"], "emp_code": code, "first_name": first, "last_name": last,
                          "department": dept, "enable_attendance": True})
    return Roster(company_id, companies[company_id], "odoo", list(departments.values()),
                  employees, terminals_for(company_id), skipped)


def _from_fixture(company_id: int) -> Roster | None:
    from tools.mock_biotime import DATASETS

    data = DATASETS.get(company_id)
    if data is None:
        return None
    return Roster(company_id, f"built-in fixture {company_id}", "fixture",
                  data["departments"], data["employees"], data["terminals"])


def load_roster(args, company_id: int, *, directory: Path | None = None) -> Roster:
    """``company_id``'s roster: the cache, else Odoo (then cached), else a fixture."""
    path = roster_path(company_id, directory)
    if path.exists() and not getattr(args, "refresh_roster", False):
        data = json.loads(path.read_text())
        return Roster(data["company_id"], data.get("company_name", ""), "cache", data.get("departments", []),
                      data["employees"], data.get("terminals") or terminals_for(company_id),
                      data.get("skipped", []))
    client = odoo_client(args, company_id)
    if client is not None:
        roster = _from_odoo(client, company_id)
        if not roster.employees:
            raise SystemExit(
                f"Company {company_id} ({roster.company_name}) has no active employee with a Badge ID, "
                "PIN or registration number in Odoo — set a Badge ID on at least one employee "
                "(Employee → HR Settings → Badge ID) and run this again.")
        path.write_text(json.dumps(roster.to_json(), indent=2) + "\n")
        return roster
    fixture = _from_fixture(company_id)
    if fixture is not None:
        return fixture
    raise SystemExit(
        f"No roster for company {company_id}: no Odoo login was found to read it from. Connect Odoo in "
        "BioBridge, or pass --odoo-url/--odoo-db/--odoo-user/--odoo-key (or set ODOO_URL etc.). "
        "Built-in fixtures exist only for companies 1, 2 and 4.")


def describe(roster: Roster) -> str:
    where = {"odoo": "read from Odoo", "cache": "cached roster", "fixture": "built-in fixture"}[roster.source]
    lines = [f"company {roster.company_id} — {roster.company_name} ({where}): "
             f"{len(roster.employees)} employee(s) on {', '.join(roster.serials)}"]
    if roster.skipped:
        shown = ", ".join(roster.skipped[:8]) + (" …" if len(roster.skipped) > 8 else "")
        lines.append(f"  skipped {len(roster.skipped)} with no Badge ID / PIN / registration number: {shown}")
    return "\n".join(lines)
