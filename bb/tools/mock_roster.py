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

Tenant scoping (mandatory)
--------------------------
``--tenant <BioBridge account slug>`` is required by both tools. Everything the
mock produces belongs to exactly one tenant: its employees come from that
tenant's own Odoo, its terminals carry the tenant in their serial number
(``MOCK-<TENANT>-C<company id>-GATE-01``), and its roster, punches and metadata
live in their own folder, ``mock_data/<tenant>/``. Nothing a tenant's mock
holds is ever read by, or shown to, another tenant's mock.

Without ``--company`` the tool covers every company that is *enabled* for that
tenant in BioBridge: all the Odoo companies when none is switched off, all but
the switched-off ones otherwise, or only the pinned one when the connection is
pinned to a single company. ``--company N`` narrows that to one, and refuses a
company that is not enabled.
"""

from __future__ import annotations

import json
import os
import re
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
    #: The Odoo server this roster was read from: {"url": ..., "db": ...}. Kept so
    #: a cached roster is never served for a different Odoo.
    odoo: dict | None = None
    #: Why a read found nothing (counts, readable fields), for the error message.
    diagnosis: str = ""
    #: Every company this roster covers: [{"id": ..., "name": ...}]. One entry
    #: for a single-company roster, several for a tenant-wide one.
    companies: list[dict] = field(default_factory=list)
    #: The BioBridge account this roster belongs to (None only for old callers).
    tenant: str | None = None

    @property
    def emp_codes(self) -> list[str]:
        return [e["emp_code"] for e in self.employees]

    @property
    def serials(self) -> list[str]:
        return [t["sn"] for t in self.terminals]

    def to_json(self) -> dict:
        return {"company_id": self.company_id, "company_name": self.company_name,
                "departments": self.departments, "employees": self.employees,
                "terminals": self.terminals, "skipped": self.skipped, "odoo": self.odoo,
                "tenant": self.tenant}


def add_odoo_arguments(parser) -> None:
    group = parser.add_argument_group("Odoo (where --company's roster is read from)")
    group.add_argument("--odoo-url", default=os.environ.get("ODOO_URL"))
    group.add_argument("--odoo-db", default=os.environ.get("ODOO_DB"))
    group.add_argument("--odoo-user", default=os.environ.get("ODOO_USER"))
    group.add_argument("--odoo-key", default=os.environ.get("ODOO_KEY"))
    group.add_argument("--tenant", default=None,
                       help="REQUIRED. The BioBridge account slug this mock belongs to: its "
                            "saved Odoo connection, enabled companies, employees, terminals "
                            "and punches are all scoped to it and invisible to every other tenant")
    group.add_argument("--data-dir", default="mock_data",
                       help="Where per-tenant mock data lives: <data-dir>/<tenant>/ "
                            "(default: ./mock_data)")
    group.add_argument("--refresh-roster", action="store_true",
                       help="Read the roster from Odoo again instead of the cached "
                            "roster_company<id>.json")
    group.add_argument("--list-companies", action="store_true",
                       help="Print the companies the Odoo login can see, and exit")


def roster_path(company_id: int, directory: Path | None = None) -> Path:
    return (directory or Path.cwd()) / f"roster_company{company_id}.json"


def tenant_token(tenant: str) -> str:
    """The tenant as it appears in a serial number: upper-case, A-Z 0-9 and
    single dashes, so ``gulf-steel`` becomes ``GULF-STEEL``."""
    return re.sub(r"[^A-Z0-9]+", "-", str(tenant).upper()).strip("-")


def terminals_for(company_id: int, tenant: str | None = None) -> list[dict]:
    base = 900 + company_id * 10
    mid = f"{tenant_token(tenant)}-" if tenant else ""
    return [
        {"id": base + 1, "sn": f"MOCK-{mid}C{company_id}-GATE-01", "alias": "Main Entrance",
         "ip_address": f"10.{company_id % 250}.0.11"},
        {"id": base + 2, "sn": f"MOCK-{mid}C{company_id}-GATE-02", "alias": "Back Door",
         "ip_address": f"10.{company_id % 250}.0.12"},
    ]


def scope_serials(terminals: list[dict], tenant: str | None) -> list[dict]:
    """Copies of ``terminals`` with the tenant in each serial number.

    Idempotent: a serial that already carries this tenant is left alone, so a
    roster read back from the cache is not prefixed twice. A serial that
    carries a *different* tenant is refused outright rather than renamed — it
    means a roster file was copied from another tenant's folder.
    """
    if not tenant:
        return [dict(t) for t in terminals]
    mid = tenant_token(tenant)
    out = []
    for term in terminals:
        sn = str(term["sn"])
        if sn.startswith(f"MOCK-{mid}-"):
            out.append(dict(term))
            continue
        other = re.match(r"MOCK-([A-Z0-9][A-Z0-9-]*?)-C\d+-GATE-\d+$", sn)
        if other and other.group(1) != mid:
            raise SystemExit(f"Terminal {sn} belongs to tenant {other.group(1)}, not {tenant} — "
                             "this roster was copied from another tenant's folder. Refusing to use it.")
        out.append({**term, "sn": sn.replace("MOCK-", f"MOCK-{mid}-", 1)})
    return out


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
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if "no such table" in text or ("relation" in text and "does not exist" in text):
            return None      # a database that BioBridge never set up: nothing saved to read
        # The database is there but could not be read, or the saved key could
        # not be decrypted. Carrying on without a login would make the mock
        # quietly serve its built-in sample people (someone else's employees,
        # terminals GATE-01..06) to a real account — so say so and stop.
        raise SystemExit(
            f"Could not read BioBridge's saved Odoo connection for "
            f"{tenant_slug or 'this account'}: {type(exc).__name__}: {exc}\n"
            "  The mock will not guess: without it, it would serve its built-in sample "
            "people instead of your Odoo's employees. Check DATABASE_URL and "
            "MASTER_ENCRYPTION_KEY, then start it again."
        ) from None


def odoo_login(args):
    """(url, db, user, key) from the flags / environment / saved connection, or None."""
    given = tuple(getattr(args, name, None) for name in ("odoo_url", "odoo_db", "odoo_user", "odoo_key"))
    if all(given):
        return given
    if any(given):
        raise SystemExit("Pass all four of --odoo-url, --odoo-db, --odoo-user and --odoo-key "
                         "(or the ODOO_* environment variables), or none of them.")
    return _saved_connection(getattr(args, "tenant", None))


def _server_of(creds) -> dict:
    return {"url": str(creds[0]).rstrip("/").lower(), "db": str(creds[1])}


def odoo_client(args, company_id: int | None):
    """An OdooClient scoped to ``company_id``, or None when no login is known."""
    creds = odoo_login(args)
    if creds is None:
        return None
    from app.integrations.odoo import OdooClient, OdooCredentials

    url, db, user, key = creds
    return OdooClient(OdooCredentials(url=url, db=db, username=user, api_key=key, company_id=company_id))


def list_companies(args) -> int:
    """Print the companies this tenant's Odoo has, marking which are enabled."""
    scope = resolve_scope(args)
    print(f"Tenant {scope.tenant}: companies of its Odoo ({scope.source})")
    enabled = {c["id"] for c in scope.companies}
    for company in scope.all_companies:
        mark = "enabled " if company["id"] in enabled else "DISABLED"
        print(f"  {company['id']:>4}  {mark}  {company['name']}")
    return 0


# --------------------------------------------------------------------------- #
# Rosters
# --------------------------------------------------------------------------- #
def _from_odoo(client, company_id: int, tenant: str | None = None) -> Roster:
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
    roster = Roster(company_id, companies[company_id], "odoo", list(departments.values()),
                    employees, terminals_for(company_id, tenant), skipped,
                    companies=[{"id": company_id, "name": companies[company_id]}], tenant=tenant)
    if not employees:
        available = client.fields_of("hr.employee")
        readable = [f for f in BADGE_FIELDS if f in available]
        with_values = sum(1 for r in rows if any(r.get(f) for f in BADGE_FIELDS))
        roster.diagnosis = (
            f"  Odoo returned {len(rows)} active employee(s) for company {company_id}; "
            f"{with_values} carry a Badge ID / PIN / registration number.\n"
            f"  Badge fields this login can read on hr.employee: {', '.join(readable) or 'NONE'}"
            + ("" if readable else
               " — the Odoo user needs the HR → Officer (or Administrator) access right to read "
               "Badge ID and PIN.")
            + ("\n  No employees at all in this company: they may belong to another company — "
               "run with --list-companies, and use the matching --company." if not rows else "")
        )
    return roster


def _from_fixture(company_id: int, tenant: str | None = None) -> Roster | None:
    from tools.mock_biotime import DATASETS

    data = DATASETS.get(company_id)
    if data is None:
        return None
    return Roster(company_id, f"built-in fixture {company_id}", "fixture",
                  data["departments"], data["employees"],
                  scope_serials(data["terminals"], tenant),
                  companies=[{"id": company_id, "name": f"built-in fixture {company_id}"}],
                  tenant=tenant)


def load_roster(args, company_id: int, *, directory: Path | None = None,
                tenant: str | None = None) -> Roster:
    """``company_id``'s roster: the cache, else Odoo (then cached), else a fixture.

    With ``tenant`` the terminals carry the tenant in their serial numbers and a
    cached roster stamped for another tenant is refused."""
    tenant = tenant or getattr(args, "tenant", None) or None
    path = roster_path(company_id, directory)
    login = odoo_login(args)
    if path.exists() and not getattr(args, "refresh_roster", False):
        data = json.loads(path.read_text())
        cached_for = data.get("odoo")
        if login is not None and cached_for != _server_of(login):
            # The cache is keyed by company id alone, and ids repeat across Odoo
            # servers (company 1 is everyone's first company). Serving it for a
            # different Odoo would hand out another database's employees.
            before = f"{cached_for['url']} / {cached_for['db']}" if cached_for else "an unrecorded Odoo"
            print(f"  ignoring {path.name}: it was read from {before}, not "
                  f"{_server_of(login)['url']} / {_server_of(login)['db']} — reading the roster again",
                  file=sys.stderr)
        else:
            if tenant and data.get("tenant") not in (None, tenant):
                raise SystemExit(f"{path} belongs to tenant {data['tenant']}, not {tenant} — "
                                 "refusing to serve one tenant's employees to another.")
            return Roster(data["company_id"], data.get("company_name", ""), "cache", data.get("departments", []),
                          data["employees"],
                          scope_serials(data.get("terminals") or terminals_for(company_id, tenant), tenant),
                          data.get("skipped", []), cached_for,
                          companies=[{"id": data["company_id"], "name": data.get("company_name", "")}],
                          tenant=tenant)
    client = odoo_client(args, company_id)
    if client is not None:
        roster = _from_odoo(client, company_id, tenant)
        roster.odoo = _server_of(login) if login else None
        if not roster.employees:
            raise SystemExit(
                f"Company {company_id} ({roster.company_name}) has no active employee with a Badge ID, "
                "PIN or registration number in Odoo — set a Badge ID on at least one employee "
                "(Employee → HR Settings → Badge ID) and run this again."
                + ("\n" + roster.diagnosis if roster.diagnosis else ""))
        path.write_text(json.dumps(roster.to_json(), indent=2) + "\n")
        return roster
    fixture = _from_fixture(company_id, tenant)
    if fixture is not None:
        return fixture
    raise SystemExit(
        f"No roster for company {company_id}: no Odoo login was found to read it from. Connect Odoo in "
        "BioBridge, or pass --odoo-url/--odoo-db/--odoo-user/--odoo-key (or set ODOO_URL etc.). "
        "Built-in fixtures exist only for companies 1, 2 and 4.")


def describe(roster: Roster) -> str:
    where = {"odoo": "read from Odoo", "cache": "cached roster", "fixture": "built-in fixture",
             "mixed": "Odoo and cache"}[roster.source]
    if roster.tenant and len(roster.companies) != 1 or (roster.tenant and roster.company_id == 0):
        per = ", ".join(f"{c['id']} {c['name']}" for c in roster.companies)
        lines = [f"tenant {roster.tenant} — {len(roster.companies)} enabled compan"
                 f"{'y' if len(roster.companies) == 1 else 'ies'} ({per}; {where}): "
                 f"{len(roster.employees)} employee(s) on {len(roster.terminals)} terminal(s): "
                 f"{', '.join(roster.serials)}"]
    else:
        lines = [f"company {roster.company_id} — {roster.company_name} ({where}): "
                 f"{len(roster.employees)} employee(s) on {', '.join(roster.serials)}"]
    if roster.skipped:
        shown = ", ".join(roster.skipped[:8]) + (" …" if len(roster.skipped) > 8 else "")
        lines.append(f"  skipped {len(roster.skipped)} with no Badge ID / PIN / registration number: {shown}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tenant scope: which tenant, which of its companies, where its data lives
# --------------------------------------------------------------------------- #
@dataclass
class TenantScope:
    tenant: str
    #: Companies enabled for this tenant: what the mock generates employees,
    #: terminals and punches for.
    companies: list[dict]
    #: Every company the tenant's Odoo has (enabled or not), for display.
    all_companies: list[dict]
    data_dir: Path
    #: "odoo" (read live) or "fixture" (no Odoo login anywhere).
    source: str

    @property
    def punches_path(self) -> Path:
        return self.data_dir / "punches.json"


def _tenant_settings(tenant: str) -> dict:
    """What BioBridge itself knows about ``tenant``: whether the account exists
    (True/False, or None when no database can be read) and its Odoo
    connection's company pin and switched-off companies."""
    result = {"known": None, "pin": None, "disabled": []}
    try:
        from app.db.session import SessionLocal
        from app.models import OdooConnection, Tenant
    except Exception:  # noqa: BLE001 — not run from a BioBridge checkout
        return result
    try:
        with SessionLocal() as db:
            slugs = sorted(t.slug for t in db.query(Tenant).all())
            row = db.query(Tenant).filter(Tenant.slug == tenant).first()
            result["known"] = row is not None
            result["slugs"] = slugs
            if row is not None:
                conn = (db.query(OdooConnection)
                        .filter(OdooConnection.tenant_id == row.id, OdooConnection.is_active.is_(True))
                        .first())
                if conn is not None:
                    result["pin"] = conn.company_id
                    result["disabled"] = [int(i) for i in (conn.disabled_company_ids or [])]
    except Exception:  # noqa: BLE001 — no database here, or it is unreadable
        result["known"] = None
    return result


def resolve_scope(args) -> TenantScope:
    """Turn ``--tenant`` (required) and the optional ``--company`` into the exact
    set of companies this run may touch, and the tenant's own data folder."""
    tenant = (getattr(args, "tenant", None) or "").strip()
    if not tenant:
        raise SystemExit("--tenant is required: every employee, terminal and punch the mock "
                         "produces belongs to one BioBridge account and is invisible to the others. "
                         "Pass the account's slug, e.g. --tenant gulf-steel.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", tenant):
        raise SystemExit(f"--tenant {tenant!r} is not a valid account slug (letters, digits, - and _ only).")

    settings = _tenant_settings(tenant)
    if settings["known"] is False:
        raise SystemExit(f"BioBridge has no account with the slug {tenant!r}. Accounts here: "
                         f"{', '.join(settings.get('slugs') or []) or 'none'}.")

    login = odoo_login(args)
    if login is not None:
        client = odoo_client(args, None)
        from app.integrations.odoo import OdooError
        try:
            client.authenticate()
            all_companies = [{"id": c["id"], "name": c["name"]} for c in client.list_companies()]
        except OdooError as exc:
            raise SystemExit(f"Could not reach Odoo: {exc}") from None
        source = "odoo"
    else:
        from tools.mock_biotime import DATASETS
        all_companies = [{"id": i, "name": f"built-in fixture {i}"} for i in sorted(DATASETS)]
        source = "fixture"

    pin, disabled = settings["pin"], set(settings["disabled"])
    if pin is not None:
        enabled = [c for c in all_companies if c["id"] == pin]
    else:
        enabled = [c for c in all_companies if c["id"] not in disabled]
    if not enabled:
        raise SystemExit(f"Tenant {tenant} has no enabled company: every company is switched off in "
                         "BioBridge (Settings → Odoo → Companies), or the pinned one is not visible.")

    wanted = getattr(args, "company", None)
    if wanted is not None:
        chosen = [c for c in enabled if c["id"] == wanted]
        if not chosen:
            exists = any(c["id"] == wanted for c in all_companies)
            listing = ", ".join(f"{c['id']} ({c['name']})" for c in enabled)
            raise SystemExit(
                (f"Company {wanted} is switched off (or not pinned) for tenant {tenant}. " if exists
                 else f"Company {wanted} does not exist for tenant {tenant}'s Odoo. ")
                + f"Enabled companies: {listing}. Omit --company to use all of them.")
        enabled = chosen

    root = Path(getattr(args, "data_dir", None) or "mock_data")
    data_dir = root / tenant
    data_dir.mkdir(parents=True, exist_ok=True)
    meta_path = data_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if meta.get("tenant") != tenant:
            raise SystemExit(f"{data_dir} holds data for tenant {meta.get('tenant')}, not {tenant} — "
                             "refusing to mix tenants. Use a different --data-dir.")
        if login is not None and meta.get("odoo") not in (None, _server_of(login)):
            raise SystemExit(f"{data_dir} was created for a different Odoo ({meta['odoo']['url']} / "
                             f"{meta['odoo']['db']}) — a tenant has exactly one. Remove the folder to start over.")
    meta_path.write_text(json.dumps({"tenant": tenant, "odoo": _server_of(login) if login else None,
                                     "companies": [c["id"] for c in enabled]}, indent=2) + "\n")
    return TenantScope(tenant, enabled, all_companies, data_dir, source)


def load_tenant_roster(args, scope: TenantScope) -> Roster:
    """One roster for every enabled company: each company's own employees, each
    on that company's own terminals. A badge that two companies both use is
    served for the first only — a BioTime server has one user per code."""
    parts = [load_roster(args, c["id"], directory=scope.data_dir, tenant=scope.tenant)
             for c in scope.companies]
    departments: dict[int, dict] = {}
    employees, terminals, skipped, claimed = [], [], [], {}
    for part in parts:
        for dept in part.departments:
            departments.setdefault(dept["id"], dept)
        for term in part.terminals:
            terminals.append({**term, "_company_id": part.company_id})
        for emp in part.employees:
            code = str(emp["emp_code"])
            if code in claimed:
                skipped.append(f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip()
                               + f" (badge {code} already used in company {claimed[code]})")
                continue
            claimed[code] = part.company_id
            employees.append({**emp, "_company_id": part.company_id})
        skipped.extend(part.skipped)
    names = ", ".join(c["name"] for c in scope.companies)
    first = parts[0]
    return Roster(
        company_id=first.company_id if len(parts) == 1 else 0,
        company_name=names, source=first.source if len({p.source for p in parts}) == 1 else "mixed",
        departments=list(departments.values()), employees=employees, terminals=terminals,
        skipped=skipped, odoo=first.odoo,
        companies=[{"id": p.company_id, "name": p.company_name} for p in parts],
        tenant=scope.tenant)
