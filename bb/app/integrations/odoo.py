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
        self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None
    ):
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

    def ping(self) -> dict[str, Any]:
        """Full readiness probe behind the Test Connection button."""
        version = self.version()
        self.authenticate()

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
        }

    # -- employees ---------------------------------------------------------
    def find_employee(self, emp_code: str) -> tuple[int | None, str | None, str | None]:
        """Resolve a badge to an hr.employee.

        Returns ``(id, name, method)``. Ambiguity is reported, not resolved: the
        caller decides, because guessing here would silently attach one person's
        attendance to another.
        """
        available = self.fields_of("hr.employee")
        ctx = {"active_test": False}

        for field_name, method in MATCH_FIELDS:
            if field_name not in available:
                continue
            found = self.execute(
                "hr.employee",
                "search_read",
                [[(field_name, "=", emp_code)]],
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
        return self.execute(
            "hr.employee",
            "search_read",
            [[("active", "in", [True, False])]],
            {"fields": wanted, "limit": limit or 0, "context": {"active_test": False}},
        ) or []

    def create_employee(self, name: str, emp_code: str) -> int:
        vals: dict[str, Any] = {"name": name or f"Employee {emp_code}"}
        available = self.fields_of("hr.employee")
        if "barcode" in available:
            vals["barcode"] = emp_code
        if "pin" in available:
            vals["pin"] = emp_code
        result = self.execute("hr.employee", "create", [vals])
        return int(result if isinstance(result, int) else result[0])

    # -- attendance --------------------------------------------------------
    def get_open_attendance(self, employee_id: int) -> dict[str, Any] | None:
        rows = self.execute(
            "hr.attendance",
            "search_read",
            [[("employee_id", "=", employee_id), ("check_out", "=", False)]],
            {"fields": ["id", "check_in"], "limit": 1, "order": "check_in desc"},
        )
        return rows[0] if rows else None

    def attendance_exists(self, employee_id: int, check_in: datetime) -> int | None:
        """Guards against duplicates if the local ledger was restored from backup."""
        rows = self.execute(
            "hr.attendance",
            "search_read",
            [[("employee_id", "=", employee_id), ("check_in", "=", fmt_dt(check_in))]],
            {"fields": ["id"], "limit": 1},
        )
        return rows[0]["id"] if rows else None

    def create_attendance(
        self,
        employee_id: int,
        check_in: datetime,
        check_out: datetime | None = None,
        biotime_ref: str | None = None,
    ) -> int:
        vals: dict[str, Any] = {"employee_id": employee_id, "check_in": fmt_dt(check_in)}
        if check_out is not None:
            vals["check_out"] = fmt_dt(check_out)
        if biotime_ref and "biotime_ref" in self.fields_of("hr.attendance"):
            vals["biotime_ref"] = biotime_ref
        result = self.execute("hr.attendance", "create", [vals])
        return int(result if isinstance(result, int) else result[0])

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
