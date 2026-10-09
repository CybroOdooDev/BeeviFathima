"""Odoo external API — JSON-2 on Odoo 19+, XML-RPC on Odoo 14 to 18.

Works against Odoo Online, Odoo.sh and self-hosted, with no module installed
on the customer side. The customer supplies:

    url       https://acme.odoo.com     (no path — see _check_url)
    db        acme
    username  integration@acme.com
    api_key   Preferences > Account Security > New API Key

Which API
---------
Odoo removes XML-RPC *and* JSON-RPC (``/xmlrpc``, ``/xmlrpc/2``, ``/jsonrpc``)
in Odoo 20. Their replacement, the JSON-2 API (``POST /json/2/<model>/<method>``
with ``Authorization: bearer <api key>``), first shipped in Odoo 19 — so no
single transport covers every supported version:

* Odoo 19 and later  → JSON-2
* Odoo 14 to 18      → XML-RPC (the only external API they have)

``OdooClient`` picks one per server on first use (see ``_detect_api``) and
remembers it for an hour, so an Odoo upgraded from 18 to 19 moves to JSON-2 on
its own. Everything above ``execute`` is unaware of the difference: callers
still pass ``execute_kw``-style positional ``args``, and ``_json2_body`` turns
them into JSON-2's named arguments.

Datetime contract: Odoo stores ``Datetime`` fields as **naive UTC**. Everything
this client sends or receives is naive UTC; conversion happens upstream in
``services/timeutils``.

A note on the error messages
----------------------------
A non-200 from either API is accurate but useless to a customer as-is: every
status has a different fix, and none of them is "check your credentials" — the
request never reached Odoo's handler, so the database, login and key have not
been tested at all. The status is mapped to an actionable sentence below,
because this is the single most common support ticket the product generates.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
import threading
import time
import xmlrpc.client
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

ODOO_DT_FMT = "%Y-%m-%d %H:%M:%S"

#: hr.employee fields to match a badge against, in priority order.
#: hr.attendance ids per search/write when linking devices to past records.
_ATTENDANCE_BATCH = 500

MATCH_FIELDS: tuple[tuple[str, str], ...] = (
    ("barcode", "barcode"),
    ("pin", "pin"),
    ("registration_number", "registration_number"),
    ("work_email", "work_email"),
)

# --------------------------------------------------------------------------- #
# Which API
# --------------------------------------------------------------------------- #
API_AUTO = "auto"
API_JSON2 = "json2"
API_XMLRPC = "xmlrpc"
API_LABELS = {API_JSON2: "JSON-2 API", API_XMLRPC: "XML-RPC"}

#: First Odoo major version with the JSON-2 API. (Some Odoo Online
#: ``saas~18.x`` builds have it too, but they still have XML-RPC as well, so
#: they stay on XML-RPC until they reach 19.)
JSON2_MIN_MAJOR = 19

#: How long a server's detected API is trusted before it is looked up again.
_API_TTL_SECONDS = 3600
_api_cache: dict[tuple[str, str], tuple[str, dict[str, Any], float]] = {}
_api_cache_lock = threading.Lock()

#: Tests point this at an httpx.MockTransport standing in for Odoo.
TRANSPORT: httpx.BaseTransport | None = None


def forget_detected_api(url: str | None = None) -> None:
    """Drop cached API choices — all of them, or one server's."""
    with _api_cache_lock:
        if url is None:
            _api_cache.clear()
        else:
            for key in [k for k in _api_cache if k[0] == url.rstrip("/")]:
                _api_cache.pop(key, None)


_TRANSPORT_HINTS: dict[int, str] = {
    301: "that URL redirects elsewhere, and Odoo's API does not follow "
         "redirects. Use the redirect target — usually the https:// form of the "
         "same host.",
    302: "that URL redirects elsewhere, and Odoo's API does not follow redirects.",
    307: "that URL redirects elsewhere, and Odoo's API does not follow redirects.",
    308: "that URL redirects permanently elsewhere, and Odoo's API does not "
         "follow redirects. Use the https:// form.",
    400: "the server rejected the request. On Odoo 17+ this is what a base URL "
         "with an extra path segment returns — enter only https://host.",
    401: "something in front of Odoo demands HTTP basic authentication, usually "
         "a protected staging site.",
    403: "a proxy, WAF or CDN is blocking Odoo's API before Odoo sees it "
         "(Cloudflare blocks XML-RPC by default). Allow /json/2/* (Odoo 19+) or "
         "/xmlrpc/2/* (older Odoo) from this server's address.",
    404: "there is no Odoo API endpoint there. The URL is wrong — most often it "
         "has a path on the end.",
    500: "Odoo itself errored on the request. Check the Odoo server log.",
    502: "a reverse proxy is up but cannot reach Odoo behind it.",
    503: "the server is refusing requests, or Odoo has no free worker.",
    504: "a reverse proxy timed out waiting for Odoo.",
}


class OdooError(RuntimeError):
    """Any failure talking to Odoo, already phrased for a human."""

    #: The HTTP status behind a transport failure, when there was one.
    http_status: int | None = None


class OdooAuthError(OdooError):
    """Bad credentials, wrong database, or insufficient rights."""


#: Distinguishes "not looked up yet" from "looked up, and there is none" for
#: OdooClient._device_mode, which otherwise couldn't tell those apart — both
#: would be spelled None.
_UNSET = object()


def _with_status(err: OdooError, status: int | None) -> OdooError:
    err.http_status = status
    return err


def _transport_error(url: str, exc: Exception, endpoint: str = "/xmlrpc/2/common") -> OdooError:
    if isinstance(exc, xmlrpc.client.ProtocolError):
        hint = _TRANSPORT_HINTS.get(
            exc.errcode, f"the endpoint answered HTTP {exc.errcode} {exc.errmsg}."
        )
        return _with_status(OdooError(
            f"Could not reach Odoo's API at {url}: {hint} "
            f"(HTTP {exc.errcode} on {endpoint})"
        ), exc.errcode)
    if isinstance(exc, xmlrpc.client.ResponseError):
        return OdooError(
            f"{url} answered, but with a web page instead of Odoo's API. Something "
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


def _http_error(url: str, exc: httpx.HTTPError) -> OdooError:
    """An httpx failure, phrased the same way as the XML-RPC ones."""
    cause = exc.__cause__ or exc.__context__
    while cause is not None and not isinstance(
        cause, (ssl.SSLError, socket.gaierror, ConnectionRefusedError, socket.timeout, TimeoutError)
    ):
        cause = cause.__cause__ or cause.__context__
    if cause is not None:
        return _transport_error(url, cause)
    if isinstance(exc, httpx.TimeoutException):
        return _transport_error(url, TimeoutError())
    text = str(exc)
    if "WRONG_VERSION_NUMBER" in text:
        return _transport_error(url, ssl.SSLError("WRONG_VERSION_NUMBER"))
    if "SSL" in text or "CERTIFICATE" in text.upper():
        return OdooError(f"TLS failed for {url}: {text}")
    if "Name or service not known" in text or "nodename nor servname" in text \
            or "getaddrinfo failed" in text or "No address associated" in text:
        return OdooError(f"Cannot resolve the host in {url}: {text}")
    if "Connection refused" in text or "actively refused" in text:
        return _transport_error(url, ConnectionRefusedError())
    return OdooError(f"Cannot reach Odoo at {url}: {text or type(exc).__name__}")


def _major(info: dict[str, Any] | None) -> int | None:
    """Odoo's major version from a version() answer: 19 for "19.0",
    18 for "saas~18.3", None if it can't be read."""
    if not info:
        return None
    for value in ((info.get("server_version_info") or [None])[0], info.get("server_version")):
        match = re.search(r"\d+", str(value or ""))
        if match:
            return int(match.group())
    return None


# --------------------------------------------------------------------------- #
# JSON-2: positional execute_kw args → named arguments
# --------------------------------------------------------------------------- #
#: method → (first positional arg is the record ids, names of the rest).
#: JSON-2 has no positional arguments at all, so every method this client (or
#: a tool built on it) calls with positional ``args`` needs its parameter
#: names here. Keyword-only calls need no entry.
_JSON2_SIGNATURES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "search": (False, ("domain", "offset", "limit", "order")),
    "search_read": (False, ("domain", "fields", "offset", "limit", "order")),
    "search_count": (False, ("domain", "limit")),
    "name_search": (False, ("name", "domain", "operator", "limit")),
    "create": (False, ("vals_list",)),
    "fields_get": (False, ("allfields", "attributes")),
    "check_access_rights": (False, ("operation", "raise_exception")),
    "has_access": (False, ("operation",)),
    "context_get": (False, ()),
    "read": (True, ("fields", "load")),
    "write": (True, ("vals",)),
    "unlink": (True, ()),
    "biobridge_upsert": (False, ("serial_number", "vals")),
}


def _json2_body(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    args = list(args or [])
    body: dict[str, Any] = {}
    if args:
        signature = _JSON2_SIGNATURES.get(method)
        if signature is None:
            raise OdooError(
                f"BioBridge can't call {model}.{method} with positional arguments on "
                "Odoo's JSON-2 API — pass them by name."
            )
        takes_ids, names = signature
        if takes_ids:
            ids = args.pop(0)
            body["ids"] = [ids] if isinstance(ids, int) else list(ids or [])
        if len(args) > len(names):
            raise OdooError(f"Too many arguments for {model}.{method}.")
        body.update(zip(names, args))
    for key, value in (kwargs or {}).items():
        if key in body:
            raise OdooError(f"{model}.{method} got {key!r} twice.")
        body[key] = value
    if method == "create" and isinstance(body.get("vals_list"), dict):
        # XML-RPC accepts one dict; JSON-2 wants the list create() takes.
        body["vals_list"] = [body["vals_list"]]
    return body


def _json2_error(url: str, model: str, method: str, db: str, resp: httpx.Response) -> OdooError:
    status = resp.status_code
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not (data.get("message") or data.get("name")):
        # Not Odoo's own error document: a proxy, a WAF, or no Odoo at all.
        hint = _TRANSPORT_HINTS.get(status, f"the endpoint answered HTTP {status}.")
        return _with_status(OdooError(
            f"Could not reach Odoo's API at {url}: {hint} "
            f"(HTTP {status} on /json/2/{model}/{method})"
        ), status)

    name = str(data.get("name") or "")
    message = str(data.get("message") or name).strip()
    lowered = message.lower()
    if status == 401 or "apikey" in lowered.replace(" ", "") or name.endswith("Unauthorized"):
        return _with_status(OdooAuthError(
            "Odoo rejected the API key — it is wrong, was revoked, or has expired "
            "(API keys on Odoo 19+ have an expiry date). Create a new one in Odoo: "
            "avatar → My Profile → Account Security → New API Key, and paste it here."
        ), status)
    if "database" in lowered and ("not found" in lowered or "does not exist" in lowered
                                  or "no database" in lowered):
        return _with_status(OdooAuthError(
            f"Odoo has no database named {db!r}. On Odoo Online the database name is "
            "usually the subdomain of the URL."
        ), status)
    if status == 403 or "AccessError" in name:
        return _with_status(OdooAuthError(
            f"The Odoo user lacks permission for {model}.{method}. Grant "
            "the 'Employees / Administrator' or HR Officer group."
            + (f" Odoo said: {message[:200]}" if message else "")
        ), status)
    return _with_status(OdooError(f"Odoo {model}.{method} failed: {message[:400]}"), status)


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
    #: Companies switched OFF for this connection (res.company ids). Every
    #: other company the Odoo user can see is on — including ones created in
    #: Odoo after this was saved, which is why the choice is stored as the
    #: exceptions rather than as a list of the enabled. Empty = all on.
    #: Combined with ``company_id`` (the older single-company pin) by
    #: OdooClient.company_scope.
    disabled_company_ids: list[int] = field(default_factory=list)
    #: "auto" (detect from the server's version), "json2" or "xmlrpc".
    api: str = API_AUTO


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


_XMLRPC_TRANSPORT_ERRORS = (
    xmlrpc.client.ProtocolError,
    xmlrpc.client.ResponseError,
    ssl.SSLError,
    OSError,
    socket.timeout,
)


class OdooClient:
    def __init__(self, creds: OdooCredentials, timeout: int | None = None) -> None:
        self.creds = creds
        self.url = _check_url(creds.url)
        self._uid: int | None = creds.uid
        self._timeout = timeout or settings.http_timeout_seconds
        self._common: xmlrpc.client.ServerProxy | None = None
        self._models: xmlrpc.client.ServerProxy | None = None
        self._http: httpx.Client | None = None
        self._api: str | None = None if (creds.api or API_AUTO) == API_AUTO else creds.api
        if self._api not in (None, API_JSON2, API_XMLRPC):
            raise OdooError(f"Unknown Odoo API {creds.api!r} — use auto, json2 or xmlrpc.")
        self._version_info: dict[str, Any] | None = None
        self._field_cache: dict[str, set[str]] = {}
        self._scope: list[int] | None | object = _UNSET
        self._scope_key: tuple | None = None
        #: Whether this Odoo keeps access rights and record rules in the one
        #: ``ir.access`` model (Odoo 20) rather than ``ir.model.access`` +
        #: ``ir.rule``. None = not looked at yet.
        self._unified_access: bool | None = None
        #: "module" | "bootstrap" | None | _UNSET (not looked up this
        #: instance's lifetime yet) — see _device_tracking_mode.
        self._device_mode: str | None | object = _UNSET
        #: pairing-method record ids by code, looked up once per client.
        self._pairing_ids: dict[str, int] = {}

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __del__(self) -> None:  # best effort; a client is cheap to leak
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # -- which API ---------------------------------------------------------
    @property
    def api(self) -> str:
        """"json2" or "xmlrpc" — detected on first use unless pinned."""
        if self._models is not None:
            # An XML-RPC ``object`` proxy was plugged in directly (tests do).
            return API_XMLRPC
        if self._api is None:
            self._api = self._detect_api()
        return self._api

    @property
    def api_label(self) -> str:
        return API_LABELS.get(self.api, self.api)

    def _detect_api(self) -> str:
        key = (self.url, self.creds.db)
        with _api_cache_lock:
            cached = _api_cache.get(key)
        if cached and cached[2] > time.monotonic():
            self._version_info = cached[1]
            return cached[0]

        info = self._web_version()
        if info is not None:
            api = API_JSON2 if (_major(info) or 0) >= JSON2_MIN_MAJOR else API_XMLRPC
        else:
            try:
                info = self._xmlrpc_version()
            except OdooError as exc:
                if exc.http_status != 404:
                    raise
                # Neither /web/version nor XML-RPC: an Odoo 20+ where only
                # JSON-2 is left. Its own calls will say so if that's wrong.
                info = {}
                api = API_JSON2
            else:
                api = API_JSON2 if (_major(info) or 0) >= JSON2_MIN_MAJOR else API_XMLRPC
        self._version_info = info
        with _api_cache_lock:
            _api_cache[key] = (api, info, time.monotonic() + _API_TTL_SECONDS)
        log.info("Odoo at %s: version %s, using %s", self.url,
                 info.get("server_version") or "unknown", API_LABELS[api])
        return api

    # -- transports --------------------------------------------------------
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
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=self._timeout,
                follow_redirects=False,
                transport=TRANSPORT,
                headers={"User-Agent": "BioBridge (Odoo attendance sync)"},
            )
        return self._http

    def _web_version(self) -> dict[str, Any] | None:
        """``GET /web/version``, normalised to XML-RPC's version() shape —
        or None where that route doesn't answer it (older Odoo)."""
        try:
            resp = self.http.get(f"{self.url}/web/version")
        except httpx.HTTPError as exc:
            raise _http_error(self.url, exc) from exc
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("version_info"):
            return None
        return {
            "server_version": data.get("version") or ".".join(str(p) for p in data["version_info"][:2]),
            "server_version_info": list(data["version_info"]),
        }

    def _xmlrpc_version(self) -> dict[str, Any]:
        try:
            return self.common.version()
        except _XMLRPC_TRANSPORT_ERRORS as exc:
            raise _transport_error(self.url, exc) from exc

    def _json2(self, model: str, method: str, body: dict[str, Any]) -> Any:
        try:
            resp = self.http.post(
                f"{self.url}/json/2/{model}/{method}",
                json=body,
                headers={
                    "Authorization": f"bearer {self.creds.api_key}",
                    "X-Odoo-Database": self.creds.db,
                },
            )
        except httpx.HTTPError as exc:
            raise _http_error(self.url, exc) from exc
        if resp.status_code != 200:
            raise _json2_error(self.url, model, method, self.creds.db, resp)
        try:
            return resp.json()
        except ValueError:
            raise OdooError(
                f"{self.url} answered, but with a web page instead of Odoo's API. "
                "Something is serving HTML where the API should be."
            ) from None

    # -- session -----------------------------------------------------------
    @property
    def uid(self) -> int:
        if self._uid is None:
            self._uid = self.authenticate()
        return self._uid

    def version(self) -> dict[str, Any]:
        if self._models is None:
            self.api  # detection reads the version on the way
        if self._version_info is None:
            self._version_info = self._xmlrpc_version()
        return self._version_info

    def authenticate(self) -> int:
        if self.api == API_JSON2:
            return self._authenticate_json2()
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
        except _XMLRPC_TRANSPORT_ERRORS as exc:
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

    def _authenticate_json2(self) -> int:
        """JSON-2 has no login step: the API key *is* the user. Find out
        which user, and make sure it is the one the customer named — a key
        created by somebody else would otherwise write attendance as them."""
        context = self._json2("res.users", "context_get", {})
        uid = context.get("uid") if isinstance(context, dict) else None
        login = (self.creds.username or "").strip()
        if uid:
            rows = self._json2("res.users", "read", {"ids": [int(uid)], "fields": ["login"]})
            actual = str((rows or [{}])[0].get("login") or "")
            if login and actual and actual.strip().lower() != login.lower():
                raise OdooAuthError(
                    f"This API key belongs to the Odoo user {actual!r}, not {login!r}. "
                    f"Enter {actual!r} as the login, or create the key while signed in "
                    f"as {login!r}."
                )
        else:
            rows = self._json2("res.users", "search_read", {
                "domain": [["login", "=", login]], "fields": ["id"], "limit": 1,
            })
            if not rows:
                raise OdooAuthError(
                    f"Odoo accepted the API key, but has no user with the login {login!r}."
                )
            uid = rows[0]["id"]
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
        scope = self.company_scope() if scope_to_company else None
        if scope is not None:
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
            ctx["allowed_company_ids"] = list(scope)
            kwargs["context"] = ctx

        if self.api == API_JSON2:
            return self._json2(model, method, _json2_body(model, method, args, kwargs))

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
        except _XMLRPC_TRANSPORT_ERRORS as exc:
            raise _transport_error(self.url, exc, "/xmlrpc/2/object") from exc

    def can(self, model: str, operation: str) -> bool | None:
        """Whether the Odoo user may ``operation`` ("create", "write", …)
        records of ``model`` — or None when Odoo won't say.

        ``check_access_rights`` is deprecated since Odoo 18 in favour of
        ``has_access``; whichever this Odoo answers is used.
        """
        attempts = [("check_access_rights", [operation], {"raise_exception": False})]
        if self.api == API_JSON2:
            attempts.append(("has_access", [operation], {}))
        for method, args, kwargs in attempts:
            try:
                return bool(self.execute(model, method, args, kwargs))
            except OdooError as exc:
                log.debug("%s.%s unavailable: %s", model, method, exc)
        return None

    # -- introspection -----------------------------------------------------
    def fields_of(self, model: str) -> set[str]:
        if model not in self._field_cache:
            data = self.execute(model, "fields_get", [[], ["type"]])
            self._field_cache[model] = set(data or {})
        return self._field_cache[model]

    def company_scope(self) -> list[int] | None:
        """The companies this connection may touch, or None for "no
        restriction" (every company the Odoo user can see).

        The first id is the default company for anything created without one
        (Odoo's ``env.company`` is the first of ``allowed_company_ids``).
        Resolved once per client: the list of companies is read from Odoo
        itself, so a company added there later is on from the start.

        Raises when nothing is left enabled — an empty ``allowed_company_ids``
        would silently fall back to *every* company, the opposite of what
        switching them all off means.
        """
        disabled = set(self.creds.disabled_company_ids or [])
        pinned = self.creds.company_id
        key = (pinned, tuple(sorted(disabled)))
        if self._scope is not _UNSET and self._scope_key == key:
            return self._scope  # type: ignore[return-value]
        scope: list[int] | None
        if pinned is not None:
            scope = [] if pinned in disabled else [pinned]
        elif disabled:
            scope = [c["id"] for c in self.list_companies() if c["id"] not in disabled]
        else:
            scope = None
        if scope is not None and not scope:
            raise OdooError(
                "Every company is switched off for this connection, so there is "
                "nothing to sync. Turn at least one on under Settings → Odoo."
            )
        self._scope, self._scope_key = scope, key
        return scope

    def _in_scope(self, field_name: str) -> list[tuple[str, str, Any]]:
        """``field_name`` restricted to the enabled companies — ``=`` for one,
        ``in`` for several, nothing when unrestricted."""
        scope = self.company_scope()
        if scope is None:
            return []
        if len(scope) == 1:
            return [(field_name, "=", scope[0])]
        return [(field_name, "in", list(scope))]

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
        can_create = self.can("hr.attendance", "create")
        return {
            "ok": True,
            "server_version": version.get("server_version"),
            "api": self.api,
            "api_label": self.api_label,
            "uid": self._uid,
            "employee_count": employee_count,
            # The one that matters: without it the connection tests green and
            # then every push fails.
            # None (Odoo wouldn't say) counts as yes: the first push then
            # fails with Odoo's own, specific permission error.
            "can_create_attendance": can_create is not False,
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
        if "company_id" in available_fields:
            return self._in_scope("company_id")
        return []

    # -- employees ---------------------------------------------------------
    def find_employee(
        self, emp_code: str, *, scoped: bool = True
    ) -> tuple[int | None, str | None, str | None]:
        """Resolve a badge to an hr.employee.

        Returns ``(id, name, method)``. Ambiguity is reported, not resolved: the
        caller decides, because guessing here would silently attach one person's
        attendance to another. Scoped to ``creds.company_id`` when set, so the
        same badge number reused in a sibling company (Odoo does not enforce
        uniqueness across companies) can never resolve to the wrong person.
        """
        available = self.fields_of("hr.employee")
        ctx = {"active_test": False}
        company_domain = self._company_domain(available) if scoped else []

        for field_name, method in MATCH_FIELDS:
            if field_name not in available:
                continue
            found = self.execute(
                "hr.employee",
                "search_read",
                [[(field_name, "=", emp_code), *company_domain]],
                {"fields": ["id", "name"], "limit": 2, "context": ctx},
                scope_to_company=scoped,
            )
            if len(found) == 1:
                return found[0]["id"], found[0]["name"], method
            if len(found) > 1:
                return None, None, f"ambiguous:{method}"
        return None, None, None

    def list_employees(self, limit: int = 0) -> list[dict[str, Any]]:
        available = self.fields_of("hr.employee")
        wanted = ["id", "name", "active", "department_id"]
        wanted += [f for f in ("company_id", "parent_id") if f in available]
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
        scope = self.company_scope()
        if scope is not None and "company_id" in available:
            vals["company_id"] = scope[0]
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

    # -- hr.attendance "mode" (who wrote the check-in / check-out) ---------
    def _attendance_modes(self) -> set[str]:
        """The values this Odoo's ``in_mode`` selection allows (empty when it
        has no such field — Odoo before 17). Asked once per client."""
        cached = getattr(self, "_modes_cache", None)
        if cached is None:
            cached = set()
            try:
                if "in_mode" in self.fields_of("hr.attendance"):
                    data = self.execute("hr.attendance", "fields_get", [["in_mode"], ["selection"]])
                    cached = {k for k, _label in (data or {}).get("in_mode", {}).get("selection", [])}
            except OdooError as exc:
                log.debug("attendance modes unavailable: %s", exc)
            self._modes_cache = cached
        return cached

    def _mode_vals(
        self, *, check_in: bool = False, check_out: bool = False,
        auto_closed: bool = False, reopen: bool = False,
    ) -> dict[str, Any]:
        """Odoo's own "Mode" for what BioBridge writes: a time that came from a
        device punch is ``technical`` (not ``manual``, Odoo's default for API
        writes); a check-out BioBridge made up itself — a shift auto-closed
        for running past the limit — is ``manual``. Nothing on an Odoo that
        has no such field or no ``technical`` choice."""
        modes = self._attendance_modes()
        if "technical" not in modes:
            return {}
        vals: dict[str, Any] = {}
        if check_in:
            vals["in_mode"] = "technical"
        if check_out:
            vals["out_mode"] = "manual" if auto_closed and "manual" in modes else "technical"
        elif reopen:
            vals["out_mode"] = False
        return vals

    def create_attendance(
        self,
        employee_id: int,
        check_in: datetime,
        check_out: datetime | None = None,
        biotime_ref: str | None = None,
        device_id: int | None = None,
        pairing_mode: str | None = None,
        auto_closed: bool = False,
    ) -> int:
        vals: dict[str, Any] = {"employee_id": employee_id, "check_in": fmt_dt(check_in)}
        vals.update(self._pairing_vals(pairing_mode))
        vals.update(self._mode_vals(check_in=True, check_out=check_out is not None, auto_closed=auto_closed))
        if check_out is not None:
            vals["check_out"] = fmt_dt(check_out)
        if biotime_ref and "biotime_ref" in self.fields_of("hr.attendance"):
            vals["biotime_ref"] = biotime_ref
        if device_id:
            field = self._attendance_device_field()
            if field:
                vals[field] = device_id
        result = self.execute("hr.attendance", "create", [vals])
        return int(result if isinstance(result, int) else result[0])

    # -- which pairing method produced a record ----------------------------
    #: code -> label of the pairing methods, as BioBridge's own Pairing
    #: settings name them.
    PAIRING_METHODS = {
        "state_based": "State Based",
        "alternating": "Alternating",
        "first_last": "First In, Last Out",
    }

    def _pairing_field(self) -> str | None:
        """The hr.attendance field that links to the pairing method, if this
        Odoo has one (add-on: ``pairing_method_id``; bootstrap:
        ``x_pairing_method_id``). Found by asking, like device tracking."""
        fields = self.fields_of("hr.attendance")
        if "pairing_method_id" in fields:
            return "pairing_method_id"
        if "x_pairing_method_id" in fields:
            return "x_pairing_method_id"
        return None

    def has_pairing_tracking(self) -> bool:
        return self._pairing_field() is not None

    def pairing_method_id(self, code: str) -> int | None:
        """The Odoo record for a pairing method, created the first time it is
        needed. None when this Odoo does not track pairing methods."""
        field = self._pairing_field()
        if field is None or code not in self.PAIRING_METHODS:
            return None
        if code in self._pairing_ids:
            return self._pairing_ids[code]
        module = field == "pairing_method_id"
        model = "biobridge.pairing.method" if module else "x_biobridge_pairing_method"
        code_f, name_f = ("code", "name") if module else ("x_code", "x_name")
        found = self.execute(
            model, "search_read", [[(code_f, "=", code)]], {"fields": ["id"], "limit": 1}
        )
        if found:
            rec_id = int(found[0]["id"])
        else:
            made = self.execute(model, "create", [{code_f: code, name_f: self.PAIRING_METHODS[code]}])
            rec_id = int(made if isinstance(made, int) else made[0])
        self._pairing_ids[code] = rec_id
        return rec_id

    def _pairing_vals(self, code: str | None) -> dict[str, Any]:
        """``{field: id}`` to merge into a create/write, or {} when there is
        nothing to record — no code given, or this Odoo does not track it."""
        if not code:
            return {}
        field = self._pairing_field()
        rec_id = self.pairing_method_id(code) if field else None
        return {field: rec_id} if field and rec_id else {}

    def attendance_pairing_methods(self, attendance_ids: list[int]) -> dict[int, str | None]:
        """attendance id -> pairing method code recorded on it (None when unset
        or untracked)."""
        field = self._pairing_field()
        if field is None or not attendance_ids:
            return {i: None for i in attendance_ids}
        rows = self.execute(
            "hr.attendance", "read", [attendance_ids], {"fields": [field]}
        )
        by_rec: dict[int, str] = {}
        out: dict[int, str | None] = {}
        for row in rows:
            ref = row.get(field)
            rec_id = ref[0] if isinstance(ref, (list, tuple)) and ref else (
                ref if isinstance(ref, int) and not isinstance(ref, bool) else None
            )
            if rec_id is None:
                out[row["id"]] = None
                continue
            if rec_id not in by_rec:
                for code in self.PAIRING_METHODS:
                    if self.pairing_method_id(code) == rec_id:
                        by_rec[rec_id] = code
                        break
            out[row["id"]] = by_rec.get(rec_id)
        return out

    def ensure_pairing_tracking_bootstrap(self) -> None:
        """Create the ``x_biobridge_pairing_method`` model, its three records,
        and the ``x_pairing_method_id`` field on ``hr.attendance`` — through
        ir.model / ir.model.fields, like the device-tracking bootstrap. A
        no-op when the add-on's own ``pairing_method_id`` is already there."""
        if "pairing_method_id" in self.fields_of("hr.attendance"):
            return
        self._assert_settings_access()
        model_id = self._ensure_custom_model("x_biobridge_pairing_method", "BioBridge Pairing Method")
        self._ensure_custom_field(model_id, "x_name", "Name", "char")
        self._ensure_custom_field(model_id, "x_code", "Code", "char")
        self._ensure_model_access(model_id, "x_biobridge_pairing_method")
        self._ensure_custom_field(
            self._model_id("hr.attendance"), "x_pairing_method_id", "Pairing Method",
            "many2one", relation="x_biobridge_pairing_method",
        )
        self._field_cache.pop("hr.attendance", None)
        self._field_cache.pop("x_biobridge_pairing_method", None)
        self._pairing_ids.clear()
        for code in self.PAIRING_METHODS:
            self.pairing_method_id(code)

    def _attendance_device_field(self) -> str | None:
        """The hr.attendance field that links to the device, for whichever
        way this Odoo got device tracking; None if it has none."""
        return {"module": "device_id", "bootstrap": "x_device_id"}.get(
            self._device_tracking_mode()
        )

    def _require_attendance_device_field(self) -> str:
        field = self._attendance_device_field()
        if field is None:
            raise OdooError(
                "Device tracking is not set up on this Odoo connection — enable it "
                "(Settings → Odoo → Enable device tracking) first."
            )
        return field

    def attendance_ids_without_device(self, attendance_ids: list[int]) -> list[int]:
        """Of these hr.attendance ids, the ones that exist and have no device.
        Batched so a long history doesn't become one enormous domain."""
        field = self._require_attendance_device_field()
        found: list[int] = []
        for start in range(0, len(attendance_ids), _ATTENDANCE_BATCH):
            chunk = attendance_ids[start:start + _ATTENDANCE_BATCH]
            rows = self.execute(
                "hr.attendance",
                "search_read",
                [[("id", "in", chunk), (field, "=", False)]],
                {"fields": ["id"], "context": {"active_test": False}},
            )
            found += [int(r["id"]) for r in rows]
        return found

    def set_attendance_device(self, attendance_ids: list[int], device_id: int) -> None:
        """Point these hr.attendance records at one device record."""
        field = self._require_attendance_device_field()
        for start in range(0, len(attendance_ids), _ATTENDANCE_BATCH):
            chunk = attendance_ids[start:start + _ATTENDANCE_BATCH]
            self.execute("hr.attendance", "write", [chunk, {field: device_id}])

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
            # biobridge_upsert, rather than a search-then-create here, so
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
            result = self.execute("biobridge.device", "biobridge_upsert", [serial_number, vals])
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
            scope = self.company_scope()
            company_domain = self._in_scope("x_company_id") if has_company_field else []
            vals = {
                k: v
                for k, v in {
                    "x_location": location,
                    "x_terminal_model": terminal_model,
                    "x_ip_address": ip_address,
                }.items()
                if v
            }
            # Only stamped on a row being created or claimed: a device found
            # in one enabled company must not be moved to another just
            # because several are on.
            new_company = scope[0] if has_company_field and scope is not None else None
            existing = self.execute(
                "x_biobridge_device",
                "search_read",
                [[("x_serial_number", "=", serial_number), *company_domain]],
                {"fields": ["id"], "limit": 1},
            )
            claimed = False
            if not existing and company_domain:
                # A row with this serial and no company at all predates this
                # connection being scoped (or predates x_company_id itself,
                # added to an older bootstrap by re-running it). Claim it —
                # the write below sets x_company_id — rather than creating a
                # second device for the same terminal: attendance already
                # recorded in Odoo points at the existing row, and a new one
                # would split that terminal's history in two. A row already
                # carrying a *different* company is left alone.
                existing = self.execute(
                    "x_biobridge_device",
                    "search_read",
                    [[("x_serial_number", "=", serial_number), ("x_company_id", "=", False)]],
                    {"fields": ["id"], "limit": 1},
                )
                claimed = bool(existing)
            if existing:
                if claimed and new_company is not None:
                    vals["x_company_id"] = new_company
                if vals:
                    self.execute("x_biobridge_device", "write", [[existing[0]["id"]], vals])
                return int(existing[0]["id"])
            if new_company is not None:
                vals["x_company_id"] = new_company
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

        # The same set-up also records which pairing method produced each
        # attendance, so a change of method part-way through stays visible.
        self.ensure_pairing_tracking_bootstrap()

    def _assert_settings_access(self) -> None:
        if self.can("ir.model", "create") is False:
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

        The domain treats an *unset* ``company_field`` as visible to
        everyone, deliberately — ``'|', (company_field, '=', False), ...``,
        not just ``(company_field, 'in', company_ids)`` alone. Unlike the
        real add-on's ``company_id`` (``required=True``, so it is always
        populated), ``x_company_id`` is optional and this rule does nothing
        to backfill it onto rows that predate the field, or onto any device
        registered by a connection whose own ``OdooConnection.company_id``
        is left unset — which the README calls out as the *right* choice
        for an ordinary single-company Odoo. Without the OR, every such row
        would read as belonging to no company the current session has, and
        a plain ``'in'`` domain would hide it from everyone rather than the
        intended nobody-restricted default — turning "isolation not
        configured" into "devices silently vanish from the list".
        """
        name = f"{model_name}.biobridge_company"
        domain = (
            f"['|', ('{company_field}', '=', False), "
            f"('{company_field}', 'in', company_ids)]"
        )
        if self._unified_access:
            # Odoo 20: a restriction is an ir.access row with no group.
            self._ensure_access_row(
                model_id, name, group_id=False, domain=domain, repair_domain=True
            )
            return
        existing = self.execute(
            "ir.rule",
            "search_read",
            [[("model_id", "=", model_id), ("name", "=", name)]],
            {"fields": ["domain_force"], "limit": 1},
        )
        if existing:
            # Repaired, not just detected: the first release of this rule
            # created it as [('x_company_id', 'in', company_ids)], with no
            # "unset is visible" half. Skipping any rule that merely exists
            # by name left that strict domain in place forever, and it makes
            # every device with no company unreadable — and every create from
            # a connection with no company_id fail — for everyone. This is
            # BioBridge's own named rule, so bringing it back to the current
            # domain on "Update setup" overrides nothing the customer made.
            if (existing[0].get("domain_force") or "").strip() != domain:
                self.execute("ir.rule", "write", [[existing[0]["id"]], {"domain_force": domain}])
            return
        self.execute(
            "ir.rule",
            "create",
            [{"name": name, "model_id": model_id, "domain_force": domain}],
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
        # Odoo 20 folded ir.model.access and ir.rule into one ir.access model,
        # so the old one answers "does not exist" there. Found by asking —
        # no version parsing — and remembered for the rule step that follows.
        existing = None
        if not self._unified_access:
            try:
                existing = self.execute(
                    "ir.model.access",
                    "search",
                    [[("model_id", "=", model_id), ("name", "=", f"{model_name}.biobridge")]],
                    {"limit": 1},
                )
                self._unified_access = False
            except OdooError as exc:
                if not self._model_missing(exc, "ir.model.access"):
                    raise
                self._unified_access = True
        if self._unified_access:
            self._ensure_access_row(
                model_id, f"{model_name}.biobridge",
                group_id=self._xmlid_to_id("base", "group_user"),
            )
            return
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

    @staticmethod
    def _model_missing(exc: OdooError, model: str) -> bool:
        """Odoo saying a model does not exist — worded differently over
        JSON-2 ("the model 'x' does not exist") and XML-RPC ("Object x doesn't
        exist"), so match on the pieces both share."""
        message = str(exc).lower()
        return model in message and "exist" in message

    def _ensure_access_row(
        self, model_id: int, name: str, *, group_id: int | bool, domain: str | None = None,
        repair_domain: bool = False,
    ) -> None:
        """Create-if-missing ``ir.access`` row (Odoo 20). With a group it is a
        permission (full CRUD for that group); with none and a domain it is a
        restriction — what ``ir.rule`` used to be."""
        existing = self.execute(
            "ir.access", "search_read",
            [[("model_id", "=", model_id), ("name", "=", name)]],
            {"fields": ["domain"], "limit": 1},
        )
        if existing:
            if repair_domain and (existing[0].get("domain") or "").strip() != (domain or ""):
                self.execute("ir.access", "write", [[existing[0]["id"]], {"domain": domain}])
            return
        vals: dict[str, Any] = {
            "name": name, "model_id": model_id, "group_id": group_id or False,
            "operation": "crud",
        }
        if domain:
            vals["domain"] = domain
        self.execute("ir.access", "create", [vals])

    def update_attendance(
        self,
        attendance_id: int,
        check_in: datetime | None = None,
        check_out: datetime | None = None,
        reopen: bool = False,
        pairing_mode: str | None = None,
        auto_closed: bool = False,
    ) -> bool:
        """Rewrite a record's times — used when a later punch moves the first
        or last of the day (first/last pairing). ``reopen`` clears the
        check-out, leaving the shift open."""
        vals: dict[str, Any] = {}
        if check_in is not None:
            vals["check_in"] = fmt_dt(check_in)
        if check_out is not None:
            vals["check_out"] = fmt_dt(check_out)
        elif reopen:
            vals["check_out"] = False
        if vals:
            vals.update(self._pairing_vals(pairing_mode))
            vals.update(self._mode_vals(
                check_in=check_in is not None, check_out=check_out is not None,
                auto_closed=auto_closed, reopen=reopen and check_out is None))
        if not vals:
            return True
        return bool(self.execute("hr.attendance", "write", [[attendance_id], vals]))

    def close_attendance(
        self, attendance_id: int, check_out: datetime, pairing_mode: str | None = None,
        auto_closed: bool = False,
    ) -> bool:
        vals = {
            "check_out": fmt_dt(check_out), **self._pairing_vals(pairing_mode),
            **self._mode_vals(check_out=True, auto_closed=auto_closed),
        }
        return bool(self.execute("hr.attendance", "write", [[attendance_id], vals]))


def _check_url(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise OdooError("The Odoo URL must start with http:// or https://")
    # Every request appends /json/2/... or /xmlrpc/2/... so a path here
    # produces /web/json/2/... and a 404. Pasting the browser address bar — which on
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
