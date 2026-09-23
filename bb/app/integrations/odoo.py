"""Odoo external API over XML-RPC.

Works against Odoo Online, Odoo.sh and self-hosted, versions 14 to 19, with no
module installed on the customer side. The customer supplies:

    url       https://acme.odoo.com     (no path — see _check_url)
    db        acme
    username  integration@acme.com
    api_key   Preferences > Account Security > New API Key

Datetime contract: Odoo stores ``Datetime`` fields as **naive UTC**. Everything
this client sends or receives is naive UTC; conversion happens upstream in
``services/timeutils``.

A note on the error messages
----------------------------
``xmlrpc.client`` raises ``ProtocolError`` for any non-200, and its repr is
accurate but useless to a customer: every status has a different fix, and none of
them is "check your credentials" — the request never reached Odoo's handler, so
the database, login and key have not been tested at all. The status is mapped to
an actionable sentence below, because this is the single most common support
ticket the product generates.
"""

from __future__ import annotations

import logging
import socket
import ssl
import xmlrpc.client
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from app.core.config import settings

log = logging.getLogger(__name__)

ODOO_DT_FMT = "%Y-%m-%d %H:%M:%S"

#: hr.employee fields to match a badge against, in priority order.
MATCH_FIELDS: tuple[tuple[str, str], ...] = (
    ("barcode", "barcode"),
    ("pin", "pin"),
    ("registration_number", "registration_number"),
    ("work_email", "work_email"),
)

_TRANSPORT_HINTS: dict[int, str] = {
    301: "that URL redirects elsewhere, and XML-RPC does not follow redirects. "
         "Use the redirect target — usually the https:// form of the same host.",
    302: "that URL redirects elsewhere, and XML-RPC does not follow redirects.",
    307: "that URL redirects elsewhere, and XML-RPC does not follow redirects.",
    308: "that URL redirects permanently elsewhere, and XML-RPC does not follow "
         "redirects. Use the https:// form.",
    400: "the server rejected the request. On Odoo 17+ this is what a base URL "
         "with an extra path segment returns — enter only https://host.",
    401: "something in front of Odoo demands HTTP basic authentication, usually "
         "a protected staging site.",
    403: "a proxy, WAF or CDN is blocking the XML-RPC endpoint before Odoo sees "
         "it. Cloudflare blocks XML-RPC by default. Allow /xmlrpc/2/* from this "
         "server's address.",
    404: "there is no XML-RPC endpoint there. The URL is wrong — most often it "
         "has a path on the end.",
    500: "Odoo itself errored on the request. Check the Odoo server log.",
    502: "a reverse proxy is up but cannot reach Odoo behind it.",
    503: "the server is refusing requests, or Odoo has no free worker.",
    504: "a reverse proxy timed out waiting for Odoo.",
}


class OdooError(RuntimeError):
    """Any failure talking to Odoo, already phrased for a human."""


class OdooAuthError(OdooError):
    """Bad credentials, wrong database, or insufficient rights."""


#: Distinguishes "not looked up yet" from "looked up, and there is none" for
#: OdooClient._device_mode, which otherwise couldn't tell those apart — both
#: would be spelled None.
_UNSET = object()


def _transport_error(url: str, exc: Exception) -> OdooError:
    if isinstance(exc, xmlrpc.client.ProtocolError):
        hint = _TRANSPORT_HINTS.get(
            exc.errcode, f"the endpoint answered HTTP {exc.errcode} {exc.errmsg}."
        )
        return OdooError(
            f"Could not reach Odoo's API at {url}: {hint} "
            f"(HTTP {exc.errcode} on /xmlrpc/2/common)"
        )
    if isinstance(exc, xmlrpc.client.ResponseError):
        return OdooError(
            f"{url} answered, but with a web page instead of XML-RPC. Something "
            "is serving HTML where the API should be."
        )
    if isinstance(exc, ssl.SSLError):
        if "WRONG_VERSION_NUMBER" in str(exc):
            return OdooError(
                f"{url} does not speak TLS on that port. Try http://, or the "
                "correct TLS port."
            )
        return OdooError(f"TLS failed for {url}: {exc}")
    if isinstance(exc, socket.gaierror):
        return OdooError(f"Cannot resolve the host in {url}: {exc}")
    if isinstance(exc, ConnectionRefusedError):
        return OdooError(f"Connection refused by {url}. Nothing is listening there.")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return OdooError(
            f"Timed out connecting to {url}. A firewall is dropping the "
            "connection, or the host is unreachable from this server."
        )
    return OdooError(f"Cannot reach Odoo at {url}: {exc}")


@dataclass
class OdooCredentials:
    url: str
    db: str
    username: str
    api_key: str
    uid: int | None = None
    #: The res.company id this connection is scoped to, or None for "every
    #: company the Odoo user can see" — the only sane default for a
    #: single-company Odoo, and the dangerous one for a multi-company
    #: instance shared across BioBridge tenants. See OdooClient.execute:
    #: every call this client makes is pinned to exactly this company when
    #: it's set, regardless of how many companies the underlying Odoo user
    #: is otherwise a member of.
    company_id: int | None = None


class _TimeoutMixin:
    """``xmlrpc.client`` exposes no timeout knob; this adds one.

    Without it a hung customer Odoo pins a worker indefinitely.
    """

    _timeout: int = 30

    def make_connection(self, host):  # type: ignore[override]
        conn = super().make_connection(host)  # type: ignore[misc]
        conn.timeout = self._timeout
        return conn


class _TimeoutTransport(_TimeoutMixin, xmlrpc.client.Transport):
    def __init__(self, timeout: int) -> None:
        super().__init__(use_datetime=False)
        self._timeout = timeout


class _TimeoutSafeTransport(_TimeoutMixin, xmlrpc.client.SafeTransport):
    def __init__(self, timeout: int) -> None:
        super().__init__(use_datetime=False)
        self._timeout = timeout


class OdooClient:
    def __init__(self, creds: OdooCredentials, timeout: int | None = None) -> None:
        self.creds = creds
        self.url = _check_url(creds.url)
        self._uid: int | None = creds.uid
        self._timeout = timeout or settings.http_timeout_seconds
        self._common: xmlrpc.client.ServerProxy | None = None
        self._models: xmlrpc.client.ServerProxy | None = None
        self._field_cache: dict[str, set[str]] = {}
        #: "module" | "bootstrap" | None | _UNSET (not looked up this
        #: instance's lifetime yet) — see _device_tracking_mode.
        self._device_mode: str | None | object = _UNSET

    # -- transport ---------------------------------------------------------
    def _proxy(self, endpoint: str) -> xmlrpc.client.ServerProxy:
        transport = (
            _TimeoutSafeTransport(self._timeout)
            if self.url.lower().startswith("https")
            else _TimeoutTransport(self._timeout)
        )
        return xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/{endpoint}", allow_none=True, transport=transport
        )

    @property
    def common(self) -> xmlrpc.client.ServerProxy:
        if self._common is None:
            self._common = self._proxy("common")
        return self._common

    @property
    def models(self) -> xmlrpc.client.ServerProxy:
        if self._models is None:
            self._models = self._proxy("object")
        return self._models

    @property
    def uid(self) -> int:
        if self._uid is None:
            self._uid = self.authenticate()
        return self._uid

    def version(self) -> dict[str, Any]:
        try:
            return self.common.version()
        except (
            xmlrpc.client.ProtocolError,
            xmlrpc.client.ResponseError,
            ssl.SSLError,
            OSError,
            socket.timeout,
        ) as exc:
            raise _transport_error(self.url, exc) from exc

    def authenticate(self) -> int:
        try:
            uid = self.common.authenticate(
                self.creds.db, self.creds.username, self.creds.api_key, {}
            )
        except xmlrpc.client.Fault as exc:
            detail = str(exc.faultString or exc)
            if "does not exist" in detail or "KeyError" in detail:
                raise OdooAuthError(
                    f"Odoo has no database named {self.creds.db!r}. On Odoo Online "
                    "the database name is usually the subdomain of the URL."
                ) from exc
            raise OdooError(
                f"Odoo refused the authentication request: "
                f"{detail.strip().splitlines()[-1][:300]}"
            ) from exc
        except (
            xmlrpc.client.ProtocolError,
            xmlrpc.client.ResponseError,
            ssl.SSLError,
            OSError,
            socket.timeout,
        ) as exc:
            raise _transport_error(self.url, exc) from exc

        if not uid:
            raise OdooAuthError(
                "Odoo rejected the credentials. The API reached Odoo, so the URL "
                "is right — the database name, login or API key is wrong, and "
                "Odoo does not say which. Note a password is refused when the "
                "user has two-factor enabled."
            )
        self._uid = int(uid)
        return self._uid

    def execute(
        self,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
        *,
        scope_to_company: bool = True,
    ):
        kwargs = dict(kwargs or {})
        if scope_to_company and self.creds.company_id is not None:
            # allowed_company_ids is how Odoo's own multi-company record
            # rules scope a call — env.companies (and so every ir.rule that
            # checks company_id in company_ids) reads it straight from the
            # context, and env.company (what a create() defaults an unset
            # company_id field to) is its first entry. Forcing it here,
            # every call, is what makes company_id on OdooCredentials a real
            # guarantee rather than a filter callers have to remember to
            # add — it holds even for a write-by-id (close_attendance) that
            # never builds a domain at all, and even for an Odoo user who is
            # technically a member of several companies.
            ctx = dict(kwargs.get("context") or {})
            ctx["allowed_company_ids"] = [self.creds.company_id]
            kwargs["context"] = ctx
        try:
            return self.models.execute_kw(
                self.creds.db, self.uid, self.creds.api_key, model, method, args, kwargs or {}
            )
        except xmlrpc.client.Fault as exc:
            message = str(exc.faultString or exc)
            if "AccessError" in message or "not allowed" in message.lower():
                raise OdooAuthError(
                    f"The Odoo user lacks permission for {model}.{method}. Grant "
                    "the 'Employees / Administrator' or HR Officer group."
                ) from exc
            raise OdooError(f"Odoo {model}.{method} failed: {message[:400]}") from exc
        except (
            xmlrpc.client.ProtocolError,
            xmlrpc.client.ResponseError,
            ssl.SSLError,
            OSError,
            socket.timeout,
        ) as exc:
            raise _transport_error(self.url, exc) from exc

    # -- introspection -----------------------------------------------------
    def fields_of(self, model: str) -> set[str]:
        if model not in self._field_cache:
            data = self.execute(model, "fields_get", [[], ["type"]])
            self._field_cache[model] = set(data or {})
        return self._field_cache[model]

    def list_companies(self) -> list[dict[str, Any]]:
        """Every res.company this Odoo user can see — deliberately not
        scoped by ``creds.company_id`` (unlike everything else this client
        does: ``scope_to_company=False``), because this is exactly the list
        a caller needs in order to *pick* one, or to see what a misconfigured
        company id should have been instead. A company's own id and name
        aren't sensitive HR data the way an employee roster is.
        """
        return (
            self.execute(
                "res.company", "search_read", [[]], {"fields": ["id", "name"]},
                scope_to_company=False,
            )
            or []
        )

    def ping(self) -> dict[str, Any]:
        """Full readiness probe behind the Test Connection button."""
        version = self.version()
        self.authenticate()

        companies = self.list_companies()
        if self.creds.company_id is not None and not any(
            c["id"] == self.creds.company_id for c in companies
        ):
            # Sending allowed_company_ids for a company this user cannot
            # see isn't a graceful "scoped to nothing" — Odoo raises. Catch
            # it here with a message that actually names the problem,
            # rather than letting every subsequent call fail as a generic
            # AccessError once employee_count below trips over it.
            raise OdooError(
                f"This Odoo user cannot see company id {self.creds.company_id}. "
                "Visible companies: "
                + ", ".join(f"{c['name']} (id {c['id']})" for c in companies)
            )

        employee_count = self.execute("hr.employee", "search_count", [[]])
        can_create = self.execute(
            "hr.attendance", "check_access_rights", ["create"], {"raise_exception": False}
        )
        return {
            "ok": True,
            "server_version": version.get("server_version"),
            "uid": self._uid,
            "employee_count": employee_count,
            # The one that matters: without it the connection tests green and
            # then every push fails.
            "can_create_attendance": bool(can_create),
            "has_companion_addon": "biotime_ref" in self.fields_of("hr.attendance"),
            "has_device_tracking": self._device_tracking_mode() is not None,
            "device_tracking_mode": self._device_tracking_mode(),
            "companies": companies,
        }

    def _company_domain(self, available_fields: set[str]) -> list[tuple[str, str, Any]]:
        """The explicit ``company_id = X`` leg of a search domain, when this
        connection is scoped to one company and the model actually carries
        that field.

        Belt-and-suspenders alongside ``execute``'s ``allowed_company_ids``
        context: that context is what makes Odoo's own record rules apply,
        but a domain condition holds even for a call that context
        injection can't reach as cleanly (an ``fields_of`` cache miss
        aside) and makes the scoping visible right here rather than only
        as an emergent effect of the transport layer.
        """
        if self.creds.company_id is not None and "company_id" in available_fields:
            return [("company_id", "=", self.creds.company_id)]
        return []

    # -- employees ---------------------------------------------------------
    def find_employee(self, emp_code: str) -> tuple[int | None, str | None, str | None]:
        """Resolve a badge to an hr.employee.

        Returns ``(id, name, method)``. Ambiguity is reported, not resolved: the
        caller decides, because guessing here would silently attach one person's
        attendance to another. Scoped to ``creds.company_id`` when set, so the
        same badge number reused in a sibling company (Odoo does not enforce
        uniqueness across companies) can never resolve to the wrong person.
        """
        available = self.fields_of("hr.employee")
        ctx = {"active_test": False}
        company_domain = self._company_domain(available)

        for field_name, method in MATCH_FIELDS:
            if field_name not in available:
                continue
            found = self.execute(
                "hr.employee",
                "search_read",
                [[(field_name, "=", emp_code), *company_domain]],
                {"fields": ["id", "name"], "limit": 2, "context": ctx},
            )
            if len(found) == 1:
                return found[0]["id"], found[0]["name"], method
            if len(found) > 1:
                return None, None, f"ambiguous:{method}"
        return None, None, None

    def list_employees(self, limit: int = 0) -> list[dict[str, Any]]:
        available = self.fields_of("hr.employee")
        wanted = ["id", "name", "active", "department_id"]
        wanted += [f for f, _ in MATCH_FIELDS if f in available]
        domain = [("active", "in", [True, False]), *self._company_domain(available)]
        return self.execute(
            "hr.employee",
            "search_read",
            [domain],
            {"fields": wanted, "limit": limit or 0, "context": {"active_test": False}},
        ) or []

    def employee_code_for(self, employee_row: dict[str, Any]) -> str | None:
        """The identifier from an ``hr.employee`` row (as ``list_employees``
        returns it) that should represent this person on a biometric
        provider — the same MATCH_FIELDS priority order ``find_employee``
        checks going the other direction, so a code minted here always
        matches straight back to this employee on the next sync.
        """
        for field_name, _method in MATCH_FIELDS:
            value = employee_row.get(field_name)
            if value:
                return str(value).strip()
        return None

    def create_employee(self, name: str, emp_code: str) -> int:
        vals: dict[str, Any] = {"name": name or f"Employee {emp_code}"}
        available = self.fields_of("hr.employee")
        if "barcode" in available:
            vals["barcode"] = emp_code
        if "pin" in available:
            vals["pin"] = emp_code
        if self.creds.company_id is not None and "company_id" in available:
            vals["company_id"] = self.creds.company_id
        result = self.execute("hr.employee", "create", [vals])
        return int(result if isinstance(result, int) else result[0])

    # -- attendance --------------------------------------------------------
    def get_open_attendance(self, employee_id: int) -> dict[str, Any] | None:
        company_domain = self._company_domain(self.fields_of("hr.attendance"))
        rows = self.execute(
            "hr.attendance",
            "search_read",
            [[("employee_id", "=", employee_id), ("check_out", "=", False), *company_domain]],
            {"fields": ["id", "check_in"], "limit": 1, "order": "check_in desc"},
        )
        return rows[0] if rows else None

    def attendance_exists(self, employee_id: int, check_in: datetime) -> int | None:
        """Guards against duplicates if the local ledger was restored from backup."""
        company_domain = self._company_domain(self.fields_of("hr.attendance"))
        rows = self.execute(
            "hr.attendance",
            "search_read",
            [[("employee_id", "=", employee_id), ("check_in", "=", fmt_dt(check_in)), *company_domain]],
            {"fields": ["id"], "limit": 1},
        )
        return rows[0]["id"] if rows else None

    def attendance_closed_at(self, employee_id: int, check_out: datetime) -> dict[str, Any] | None:
        """The record, if any, already closed with exactly this check-out.

        Guards a narrower, nastier case than ``attendance_exists``: a
        check-out punch whose Odoo write (closing some shift) went through,
        but whose local commit marking that punch synced was lost before it
        landed — a crash between the two. Replayed with no open shift left
        to close (Odoo already shows it closed), that punch reads as an
        unrelated fresh check-in and would open a phantom record right next
        to the real, already-correct one. A match here means the punch
        already did its job in an earlier attempt; see
        ``SyncEngine._recover_from_lost_close``.
        """
        company_domain = self._company_domain(self.fields_of("hr.attendance"))
        rows = self.execute(
            "hr.attendance",
            "search_read",
            [[("employee_id", "=", employee_id), ("check_out", "=", fmt_dt(check_out)), *company_domain]],
            {"fields": ["id", "check_in"], "limit": 1},
        )
        return rows[0] if rows else None

    def create_attendance(
        self,
        employee_id: int,
        check_in: datetime,
        check_out: datetime | None = None,
        biotime_ref: str | None = None,
        device_id: int | None = None,
    ) -> int:
        vals: dict[str, Any] = {"employee_id": employee_id, "check_in": fmt_dt(check_in)}
        if check_out is not None:
            vals["check_out"] = fmt_dt(check_out)
        if biotime_ref and "biotime_ref" in self.fields_of("hr.attendance"):
            vals["biotime_ref"] = biotime_ref
        if device_id:
            mode = self._device_tracking_mode()
            field = {"module": "device_id", "bootstrap": "x_device_id"}.get(mode)
            if field:
                vals[field] = device_id
        result = self.execute("hr.attendance", "create", [vals])
        return int(result if isinstance(result, int) else result[0])

    # -- device tracking: two ways to get there, one call site -------------
    #
    # "module": the odoo_addon/biobridge_attendance/ add-on is installed —
    # a real Python module, only possible on Odoo.sh or self-hosted.
    #
    # "bootstrap": no add-on at all. BioBridge creates an ``x_``-prefixed
    # custom model and fields itself, purely through the external API —
    # ir.model / ir.model.fields / ir.model.access records, the same
    # mechanism Odoo Studio's UI writes to. This is what actually reaches
    # Odoo Online, which refuses to install any module that isn't from the
    # official Apps Store but places no such restriction on ordinary data
    # writes through the API a sufficiently privileged user already has.
    #
    # Both are detected the same way has_companion_addon always has been —
    # by checking which field is present on hr.attendance — rather than
    # storing a flag that could go stale if a customer's Odoo changes under
    # BioBridge between syncs.
    def _device_tracking_mode(self) -> str | None:
        if self._device_mode is _UNSET:
            fields = self.fields_of("hr.attendance")
            if "device_id" in fields:
                self._device_mode = "module"
            elif "x_device_id" in fields:
                self._device_mode = "bootstrap"
            else:
                self._device_mode = None
        return self._device_mode

    def upsert_device(
        self,
        serial_number: str,
        name: str | None = None,
        location: str | None = None,
        terminal_model: str | None = None,
        ip_address: str | None = None,
    ) -> int:
        """Find-or-create the device record for a terminal, however this
        connection's Odoo got its device tracking. Callers gate this on
        ``OdooConnection.has_device_tracking`` first; calling it when
        neither mechanism is present raises OdooError.
        """
        mode = self._device_tracking_mode()
        if mode == "module":
            # Delegates the actual find-or-create to the model's own
            # _biobridge_upsert, rather than a search-then-create here, so
            # two near-simultaneous pushes for a brand new terminal can't
            # create it twice — see that method's docstring in the add-on.
            vals: dict[str, Any] = {}
            if name:
                vals["name"] = name
            if location:
                vals["location"] = location
            if terminal_model:
                vals["terminal_model"] = terminal_model
            if ip_address:
                vals["ip_address"] = ip_address
            result = self.execute("biobridge.device", "_biobridge_upsert", [serial_number, vals])
            return int(result)

        if mode == "bootstrap":
            # No add-on means no server-side upsert method exists to
            # delegate to — the search-then-create race this avoids on the
            # module path is accepted here instead. In practice it only
            # bites two syncs racing on the very first punch a brand new
            # terminal ever produces, and BioBridge's own per-run cache
            # (SyncEngine._odoo_device_id) already keeps one run from
            # calling this twice for the same terminal.
            #
            # x_biobridge_device is a plain custom model with no built-in
            # multi-company rule of its own (unlike biobridge.device in the
            # add-on, which the "module" branch above delegates company
            # scoping to entirely) — so unlike everywhere else in this
            # client, the company condition here is load-bearing, not just
            # defense-in-depth: without it, two companies bootstrapped on
            # the same Odoo would find and overwrite each other's device
            # with the same serial number.
            has_company_field = "x_company_id" in self.fields_of("x_biobridge_device")
            company_domain = (
                [("x_company_id", "=", self.creds.company_id)]
                if has_company_field and self.creds.company_id is not None
                else []
            )
            vals = {
                k: v
                for k, v in {
                    "x_location": location,
                    "x_terminal_model": terminal_model,
                    "x_ip_address": ip_address,
                }.items()
                if v
            }
            if has_company_field and self.creds.company_id is not None:
                vals["x_company_id"] = self.creds.company_id
            existing = self.execute(
                "x_biobridge_device",
                "search_read",
                [[("x_serial_number", "=", serial_number), *company_domain]],
                {"fields": ["id"], "limit": 1},
            )
            if existing:
                if vals:
                    self.execute("x_biobridge_device", "write", [[existing[0]["id"]], vals])
                return int(existing[0]["id"])
            vals["x_serial_number"] = serial_number
            vals["x_name"] = name or serial_number
            result = self.execute("x_biobridge_device", "create", [vals])
            return int(result if isinstance(result, int) else result[0])

        raise OdooError(
            "Device tracking is not set up on this Odoo connection — install "
            "the biobridge_attendance add-on, or run the device-tracking "
            "bootstrap, first."
        )

    # -- bootstrap: create device tracking with no add-on, over the API ----
    def ensure_device_tracking_bootstrap(self) -> None:
        """Create the ``x_biobridge_device`` model, the ``x_device_id`` /
        ``x_device_location`` fields on ``hr.attendance``, and a company-scoped
        ir.rule on ``x_biobridge_device`` — purely through ir.model /
        ir.model.fields / ir.model.access / ir.rule writes, the same thing
        Odoo Studio's UI does when someone drags a field onto a form there
        (or ticks "Multi Company" on a model), just driven from here instead.
        No module, no install step, so this works on Odoo Online.

        Idempotent — each step checks for its own record before creating
        it, so a call that fails partway through (a transient error, or the
        permission check below firing on a later step some other way) can
        simply be retried. Not a no-op once bootstrap mode already exists,
        though: it still runs, purely so a connection bootstrapped before
        ``x_company_id`` or the ir.rule existed picks up whichever is
        missing on the next call rather than being stuck without it
        forever. A "module"-mode connection (the real add-on installed) is
        the only true no-op — there's nothing here for it to create.

        Needs the connected user to hold Settings/Administrator access
        (``base.group_system``) — the same level Studio itself requires.
        Checked up front, in one cheap call, so a connection lacking it
        fails with one clear message instead of leaving a half-created
        model behind.
        """
        if self._device_tracking_mode() == "module":
            return

        self._assert_settings_access()

        device_model_id = self._ensure_custom_model("x_biobridge_device", "BioBridge Device")
        for name, description, ttype, extra in (
            ("x_name", "Name", "char", {}),
            ("x_serial_number", "Serial Number", "char", {}),
            ("x_location", "Location", "char", {}),
            ("x_terminal_model", "Model", "char", {}),
            ("x_ip_address", "IP Address", "char", {}),
            # Many2one so Odoo enforces it's a real company id, and so it
            # reads the same way in the Studio UI as any other company
            # field would. Added even on an Odoo with only one company —
            # cheap, and it means a company added there later doesn't need
            # this bootstrap re-run for isolation to already be in place.
            ("x_company_id", "Company", "many2one", {"relation": "res.company"}),
        ):
            self._ensure_custom_field(device_model_id, name, description, ttype, **extra)
        self._ensure_model_access(device_model_id, "x_biobridge_device")
        # x_company_id being set on each row does nothing by itself in
        # Odoo's own UI — access rights control whether a user can read the
        # model at all, not which rows of it they see. Without this rule, a
        # person on Company A can still browse Company B's devices in the
        # Studio-generated list. Same shape as the real add-on's ir.rule
        # (odoo_addon/biobridge_attendance/security/biobridge_device_security.xml) —
        # see that file's comment for why no groups_id and what company_ids is.
        self._ensure_company_rule(device_model_id, "x_biobridge_device", "x_company_id")

        attendance_model_id = self._model_id("hr.attendance")
        self._ensure_custom_field(
            attendance_model_id, "x_device_id", "Biometric Device", "many2one",
            relation="x_biobridge_device",
        )
        self._ensure_custom_field(
            attendance_model_id, "x_device_location", "Device Location", "char",
            related="x_device_id.x_location", store=True,
        )

        # These two just learned about fields that didn't exist a moment
        # ago — without this, _device_tracking_mode and any create_attendance
        # call in the rest of this same process would keep reading the
        # pre-bootstrap answer for as long as this OdooClient instance lives.
        self._field_cache.pop("hr.attendance", None)
        self._field_cache.pop("x_biobridge_device", None)
        self._device_mode = _UNSET

    def _assert_settings_access(self) -> None:
        ok = self.execute(
            "ir.model", "check_access_rights", ["create"], {"raise_exception": False}
        )
        if not ok:
            raise OdooAuthError(
                "Setting up device tracking needs the Odoo user BioBridge connects "
                "as to have 'Settings' access — Settings > Users & Companies > "
                "Users > that user > set 'Administration' to 'Settings'. This is "
                "the same access level Odoo Studio itself requires, since this "
                "uses the same underlying mechanism Studio does."
            )

    def _model_id(self, model_name: str) -> int:
        rows = self.execute("ir.model", "search", [[("model", "=", model_name)]], {"limit": 1})
        if not rows:
            raise OdooError(f"Odoo has no model named {model_name!r}.")
        return rows[0]

    def _ensure_custom_model(self, model_name: str, label: str) -> int:
        rows = self.execute("ir.model", "search", [[("model", "=", model_name)]], {"limit": 1})
        if rows:
            return rows[0]
        result = self.execute("ir.model", "create", [{"name": label, "model": model_name}])
        return int(result if isinstance(result, int) else result[0])

    def _ensure_custom_field(
        self,
        model_id: int,
        name: str,
        field_description: str,
        ttype: str,
        *,
        relation: str | None = None,
        related: str | None = None,
        store: bool | None = None,
    ) -> None:
        found = self.execute(
            "ir.model.fields",
            "search",
            [[("model_id", "=", model_id), ("name", "=", name)]],
            {"limit": 1},
        )
        if found:
            return
        vals: dict[str, Any] = {
            "model_id": model_id,
            "name": name,
            "field_description": field_description,
            "ttype": ttype,
        }
        if relation:
            vals["relation"] = relation
        if related:
            vals["related"] = related
        if store is not None:
            vals["store"] = store
        self.execute("ir.model.fields", "create", [vals])

    def _ensure_company_rule(self, model_id: int, model_name: str, company_field: str) -> None:
        """A global ir.rule scoping every row of a bootstrap model to the
        session's active companies — the same row-level filter a real
        installed add-on gets from its own security XML, built here through
        the same create-if-missing calls the rest of bootstrap mode uses.

        No ``groups_id``: an ir.rule with none set applies globally, to
        every user, not just members of some group — the standard shape for
        "company-owned record, filtered by whichever companies the session
        has active" that most of Odoo's own multi-company models use.
        ``company_ids`` is not defined anywhere in this codebase — it's a
        variable Odoo's ir.rule evaluation always makes available to
        ``domain_force``, resolving to the companies enabled for whoever (or
        whatever XML-RPC caller) is running the request.
        """
        name = f"{model_name}.biobridge_company"
        existing = self.execute(
            "ir.rule", "search", [[("model_id", "=", model_id), ("name", "=", name)]], {"limit": 1}
        )
        if existing:
            return
        self.execute(
            "ir.rule",
            "create",
            [
                {
                    "name": name,
                    "model_id": model_id,
                    "domain_force": f"[('{company_field}', 'in', company_ids)]",
                }
            ],
        )

    def _xmlid_to_id(self, module: str, name: str) -> int | None:
        rows = self.execute(
            "ir.model.data",
            "search_read",
            [[("module", "=", module), ("name", "=", name)]],
            {"fields": ["res_id"], "limit": 1},
        )
        return rows[0]["res_id"] if rows else None

    def _ensure_model_access(self, model_id: int, model_name: str) -> None:
        """Full CRUD, for every internal user — one row, deliberately.

        The module version (odoo_addon/biobridge_attendance/) splits this
        into a read tier and a manager-only write tier, because that add-on
        ships to a customer whose own group hierarchy is stable and known.
        Here, the connected API user's own group membership is not
        something this code can assume beyond "privileged enough to create
        attendance" (see the can_create_attendance check in ping()) — and
        locking BioBridge's own account out of the table it exists to write
        to would be a worse failure mode than an inventory of terminal
        names and locations being broadly readable and writable. It isn't
        sensitive HR data.
        """
        existing = self.execute(
            "ir.model.access",
            "search",
            [[("model_id", "=", model_id), ("name", "=", f"{model_name}.biobridge")]],
            {"limit": 1},
        )
        if existing:
            return
        self.execute(
            "ir.model.access",
            "create",
            [
                {
                    "name": f"{model_name}.biobridge",
                    "model_id": model_id,
                    "group_id": self._xmlid_to_id("base", "group_user"),
                    "perm_read": 1,
                    "perm_write": 1,
                    "perm_create": 1,
                    "perm_unlink": 1,
                }
            ],
        )

    def close_attendance(self, attendance_id: int, check_out: datetime) -> bool:
        return bool(
            self.execute(
                "hr.attendance", "write", [[attendance_id], {"check_out": fmt_dt(check_out)}]
            )
        )


def _check_url(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise OdooError("The Odoo URL must start with http:// or https://")
    # Every request appends /xmlrpc/2/... so a path here produces
    # /web/xmlrpc/2/common and a 404. Pasting the browser address bar — which on
    # Odoo 17+ always carries /odoo — is the easiest mistake on this form, so it
    # is caught at construction with the corrected URL in the message.
    if parsed.path not in ("", "/"):
        raise OdooError(
            f"The Odoo URL must be just the server address, with no path. "
            f"Remove {parsed.path!r} — enter {parsed.scheme}://{parsed.netloc} instead."
        )
    return url


def fmt_dt(value: datetime) -> str:
    """Serialise a naive-UTC datetime the way Odoo expects."""
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return value.strftime(ODOO_DT_FMT)


def parse_dt(value: str | bool | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    return datetime.strptime(value[:19], ODOO_DT_FMT)
