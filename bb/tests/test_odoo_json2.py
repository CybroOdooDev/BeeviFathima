"""OdooClient over Odoo's JSON-2 API (Odoo 19+), and how it picks an API.

Odoo 20 removes XML-RPC and JSON-RPC; JSON-2 (``POST /json/2/<model>/<method>``,
``Authorization: bearer <key>``, named arguments only) replaces them and exists
from Odoo 19. So the client detects the server's version and uses JSON-2 on
19+, XML-RPC on 14–18.

The fake server below is deliberately strict about JSON-2's rules — no
positional arguments, the ORM's real parameter names, a bearer key, the
database header — and translates each call back into the same in-memory
``FakeXmlRpcModels`` the XML-RPC tests use, so the very same scenarios are
proven over both transports.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from app.integrations import odoo as odoo_mod
from app.integrations.odoo import (
    API_JSON2,
    API_XMLRPC,
    OdooAuthError,
    OdooClient,
    OdooCredentials,
    OdooError,
    _json2_body,
)
from tests.test_odoo_company_scoping import COMPANY_A, COMPANY_B, FakeXmlRpcModels

KEY = "k-json2"
DB = "acme"
UID = 7

#: The real ORM signatures (Odoo 19), independent of the client's own table —
#: so a wrong name in the client fails here instead of on a customer's Odoo.
REAL = {
    "search_read": (False, ("domain", "fields", "offset", "limit", "order")),
    "search_count": (False, ("domain", "limit")),
    "search": (False, ("domain", "offset", "limit", "order")),
    "create": (False, ("vals_list",)),
    "write": (True, ("vals",)),
    "read": (True, ("fields", "load")),
    "fields_get": (False, ("allfields", "attributes")),
    "check_access_rights": (False, ("operation", "raise_exception")),
    "has_access": (False, ("operation",)),
    "context_get": (False, ()),
}


class FakeOdoo19:
    """An Odoo 19 that only speaks JSON-2 (plus GET /web/version)."""

    def __init__(self, models: FakeXmlRpcModels | None = None, *, version=(19, 0),
                 login="bot", web_version=True, check_access_rights=True) -> None:
        self.models = models or FakeXmlRpcModels()
        self.version = version
        self.login = login
        self.web_version = web_version
        self.check_access_rights = check_access_rights
        self.requests: list[tuple[str, str, dict]] = []
        self.version_hits = 0
        self.fail: dict[str, httpx.Response] = {}

    @staticmethod
    def _error(status, name, message):
        return httpx.Response(status, json={"name": name, "message": message,
                                            "arguments": [message], "context": {}, "debug": ""})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/web/version":
            self.version_hits += 1
            if not self.web_version:
                return httpx.Response(404, text="<html>Not Found</html>")
            return httpx.Response(200, json={"version_info": [*self.version, 0, "final", 0, ""],
                                             "version": f"{self.version[0]}.{self.version[1]}"})
        if not path.startswith("/json/2/"):
            return httpx.Response(404, text="<html>Not Found</html>")
        _, _, _, model, method = path.split("/", 4)
        if f"{model}.{method}" in self.fail:
            return self.fail[f"{model}.{method}"]

        assert request.method == "POST"
        if request.headers.get("authorization") != f"bearer {KEY}":
            return self._error(401, "werkzeug.exceptions.Unauthorized", "Invalid apikey")
        assert request.headers.get("x-odoo-database") == DB
        body = json.loads(request.content)
        assert isinstance(body, dict), "JSON-2 takes a JSON object, never a list"
        self.requests.append((model, method, body))

        if model == "res.users" and method == "context_get":
            return httpx.Response(200, json={"lang": "en_US", "tz": "UTC", "uid": UID})
        if model == "res.users" and method == "read":
            assert body["ids"] == [UID]
            return httpx.Response(200, json=[{"id": UID, "login": self.login}])
        if method == "check_access_rights" and not self.check_access_rights:
            return self._error(404, "werkzeug.exceptions.NotFound", "The method does not exist")
        if method == "has_access":
            return httpx.Response(200, json=True)

        takes_ids, names = REAL[method]
        body = dict(body)
        context = body.pop("context", None)
        unknown = set(body) - set(names) - ({"ids"} if takes_ids else set())
        assert not unknown, f"{model}.{method} got unknown argument(s) {unknown}"
        args = []
        if takes_ids:
            args.append(body.pop("ids"))
        kwargs = {"context": context} if context else {}
        if method == "create":
            vals_list = body.pop("vals_list")
            assert isinstance(vals_list, list), "create() takes vals_list, a list"
            ids = [self.models.execute_kw(DB, UID, KEY, model, "create", [vals], dict(kwargs))
                   for vals in vals_list]
            return httpx.Response(200, json=ids)
        # Leading parameters go positionally to the in-memory fake, the
        # rest as keywords — the shape its execute_kw expects.
        for name in names:
            if name in body and name in ("domain", "vals", "operation"):
                args.append(body.pop(name))
        kwargs.update(body)
        result = self.models.execute_kw(DB, UID, KEY, model, method, args, kwargs)
        return httpx.Response(200, json=result)


@pytest.fixture(autouse=True)
def _fresh_api_cache():
    odoo_mod.forget_detected_api()
    yield
    odoo_mod.forget_detected_api()


def use(monkeypatch, server) -> None:
    monkeypatch.setattr(odoo_mod, "TRANSPORT", httpx.MockTransport(server))


def client(company_id=None, **creds) -> OdooClient:
    return OdooClient(OdooCredentials(url="https://acme.odoo.com", db=DB, username="bot",
                                      api_key=KEY, company_id=company_id, **creds))


# --------------------------------------------------------------------------- #
# Choosing the API
# --------------------------------------------------------------------------- #
def test_odoo_19_uses_json2(monkeypatch):
    use(monkeypatch, FakeOdoo19())
    c = client()
    assert c.api == API_JSON2
    assert c.version()["server_version"] == "19.0"


@pytest.mark.parametrize("major", [14, 16, 17, 18])
def test_odoo_before_19_stays_on_xmlrpc(monkeypatch, major):
    use(monkeypatch, FakeOdoo19(version=(major, 0)))
    assert client().api == API_XMLRPC


def test_no_web_version_falls_back_to_xmlrpc_version(monkeypatch):
    """Odoo versions whose /web/version doesn't answer a GET are read over
    XML-RPC's common.version() instead."""
    use(monkeypatch, FakeOdoo19(web_version=False))
    monkeypatch.setattr(OdooClient, "_xmlrpc_version",
                        lambda self: {"server_version": "15.0", "server_version_info": [15, 0, 0]})
    assert client().api == API_XMLRPC


def test_saas_versions_are_parsed(monkeypatch):
    use(monkeypatch, FakeOdoo19(web_version=False))
    monkeypatch.setattr(OdooClient, "_xmlrpc_version",
                        lambda self: {"server_version": "saas~19.2", "server_version_info": ["saas~19", 2]})
    assert client().api == API_JSON2


def test_no_xmlrpc_at_all_means_json2(monkeypatch):
    """Odoo 20+: XML-RPC is gone (404). Only JSON-2 is left."""
    use(monkeypatch, FakeOdoo19(web_version=False))

    def gone(self):
        err = OdooError("no XML-RPC")
        err.http_status = 404
        raise err

    monkeypatch.setattr(OdooClient, "_xmlrpc_version", gone)
    assert client().api == API_JSON2


def test_detection_is_cached_per_server_and_can_be_forgotten(monkeypatch):
    server = FakeOdoo19()
    use(monkeypatch, server)
    assert client().api == API_JSON2
    assert client().api == API_JSON2
    assert server.version_hits == 1
    odoo_mod.forget_detected_api("https://acme.odoo.com/")
    assert client().api == API_JSON2
    assert server.version_hits == 2


def test_api_can_be_pinned(monkeypatch):
    server = FakeOdoo19()
    use(monkeypatch, server)
    assert client(api="xmlrpc").api == API_XMLRPC
    assert client(api="json2").api == API_JSON2
    assert server.version_hits == 0
    with pytest.raises(OdooError, match="Unknown Odoo API"):
        client(api="jsonrpc")


# --------------------------------------------------------------------------- #
# The whole client over JSON-2
# --------------------------------------------------------------------------- #
def test_ping_over_json2(monkeypatch):
    server = FakeOdoo19()
    use(monkeypatch, server)
    info = client(company_id=COMPANY_A).ping()
    assert info["ok"] is True
    assert info["api"] == API_JSON2 and info["api_label"] == "JSON-2 API"
    assert info["uid"] == UID
    assert info["server_version"] == "19.0"
    assert info["can_create_attendance"] is True
    assert {c["id"] for c in info["companies"]} == {COMPANY_A, COMPANY_B}


def test_has_access_is_used_where_check_access_rights_is_gone(monkeypatch):
    server = FakeOdoo19(check_access_rights=False)
    use(monkeypatch, server)
    assert client().can("hr.attendance", "create") is True
    assert ("hr.attendance", "has_access", {"operation": "create"}) in server.requests


def test_attendance_round_trip_over_json2_is_company_scoped(monkeypatch):
    fake = FakeXmlRpcModels()
    fake.hr_employee[1] = {"id": 1, "name": "Employee A", "barcode": "1001",
                           "company_id": COMPANY_A, "active": True}
    fake.hr_employee[2] = {"id": 2, "name": "Employee B", "barcode": "1001",
                           "company_id": COMPANY_B, "active": True}
    server = FakeOdoo19(fake)
    use(monkeypatch, server)
    c = client(company_id=COMPANY_A)

    emp_id, name, how = c.find_employee("1001")
    assert (emp_id, name, how) == (1, "Employee A", "barcode")

    att = c.create_attendance(1, datetime(2026, 10, 5, 8, 0))
    assert fake.hr_attendance[att]["check_in"] == "2026-10-05 08:00:00"
    assert c.close_attendance(att, datetime(2026, 10, 5, 17, 0)) is True
    assert fake.hr_attendance[att]["check_out"] == "2026-10-05 17:00:00"

    new_id = c.create_employee("New Person", "2002")
    assert fake.hr_employee[new_id]["barcode"] == "2002"
    assert fake.hr_employee[new_id]["company_id"] == COMPANY_A

    # Every model call carried the company pin, as named JSON-2 arguments.
    model_calls = [(m, meth, b) for m, meth, b in server.requests if m != "res.users"]
    assert model_calls
    for model, method, body in model_calls:
        if model != "res.company":
            assert body["context"]["allowed_company_ids"] == [COMPANY_A], (model, method)
    writes = [b for m, meth, b in model_calls if meth == "write"]
    assert writes == [{"ids": [att], "vals": {"check_out": "2026-10-05 17:00:00"},
                       "context": {"allowed_company_ids": [COMPANY_A]}}]


def test_bootstrap_device_upsert_over_json2(monkeypatch):
    fake = FakeXmlRpcModels()
    use(monkeypatch, FakeOdoo19(fake))
    c = client(company_id=COMPANY_A)
    first = c.upsert_device("SN-1", name="Gate")
    again = c.upsert_device("SN-1", name="Gate", location="North wing")
    assert first == again
    assert fake.x_biobridge_device[first]["x_name"] == "Gate"
    assert fake.x_biobridge_device[first]["x_location"] == "North wing"
    assert fake.x_biobridge_device[first]["x_company_id"] == COMPANY_A


# --------------------------------------------------------------------------- #
# Errors, phrased for a customer
# --------------------------------------------------------------------------- #
def test_bad_or_expired_key(monkeypatch):
    use(monkeypatch, FakeOdoo19())
    c = OdooClient(OdooCredentials(url="https://acme.odoo.com", db=DB, username="bot", api_key="nope"))
    with pytest.raises(OdooAuthError, match="expired"):
        c.authenticate()


def test_key_of_another_user_is_refused(monkeypatch):
    use(monkeypatch, FakeOdoo19(login="someone.else@acme.com"))
    with pytest.raises(OdooAuthError, match="belongs to the Odoo user 'someone.else@acme.com'"):
        client().authenticate()


def test_login_comparison_ignores_case(monkeypatch):
    use(monkeypatch, FakeOdoo19(login="BOT"))
    assert client().authenticate() == UID


def test_access_error(monkeypatch):
    server = FakeOdoo19()
    server.fail["hr.attendance.create"] = FakeOdoo19._error(
        403, "odoo.exceptions.AccessError", "You are not allowed to create 'Attendance' records.")
    use(monkeypatch, server)
    with pytest.raises(OdooAuthError, match="lacks permission for hr.attendance.create"):
        client().create_attendance(1, datetime(2026, 10, 5, 8, 0))


def test_validation_error_keeps_odoos_message(monkeypatch):
    server = FakeOdoo19()
    server.fail["hr.attendance.create"] = FakeOdoo19._error(
        422, "odoo.exceptions.ValidationError", "Cannot create new attendance record for Ann")
    use(monkeypatch, server)
    with pytest.raises(OdooError, match="Cannot create new attendance record for Ann"):
        client().create_attendance(1, datetime(2026, 10, 5, 8, 0))


def test_waf_block_names_the_json2_path(monkeypatch):
    server = FakeOdoo19()
    server.fail["res.users.context_get"] = httpx.Response(403, text="<html>Attention Required! | Cloudflare</html>")
    use(monkeypatch, server)
    with pytest.raises(OdooError, match=r"/json/2/\*") as info:
        client().authenticate()
    assert info.value.http_status == 403


def test_unreachable_host(monkeypatch):
    def down(request):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    use(monkeypatch, down)
    with pytest.raises(OdooError, match="Connection refused"):
        client().api  # noqa: B018 — detection is the first request


def test_html_where_the_api_should_be(monkeypatch):
    server = FakeOdoo19()
    server.fail["hr.employee.search_count"] = httpx.Response(200, text="<html>login</html>")
    use(monkeypatch, server)
    with pytest.raises(OdooError, match="web page instead of Odoo's API"):
        client().execute("hr.employee", "search_count", [[]])


# --------------------------------------------------------------------------- #
# Positional → named
# --------------------------------------------------------------------------- #
def test_json2_body_names_positional_arguments():
    assert _json2_body("hr.employee", "search_read", [[("a", "=", 1)]], {"fields": ["id"], "limit": 1}) == {
        "domain": [("a", "=", 1)], "fields": ["id"], "limit": 1}
    assert _json2_body("hr.attendance", "write", [[5], {"x": 1}], {}) == {"ids": [5], "vals": {"x": 1}}
    assert _json2_body("res.users", "read", [[7]], {"fields": ["login"]}) == {"ids": [7], "fields": ["login"]}
    assert _json2_body("hr.attendance", "unlink", [[1, 2]], {}) == {"ids": [1, 2]}
    assert _json2_body("hr.employee", "create", [{"name": "A"}], {}) == {"vals_list": [{"name": "A"}]}
    assert _json2_body("hr.employee", "create", [[{"name": "A"}, {"name": "B"}]], {}) == {
        "vals_list": [{"name": "A"}, {"name": "B"}]}
    assert _json2_body("hr.attendance", "check_access_rights", ["create"], {"raise_exception": False}) == {
        "operation": "create", "raise_exception": False}


def test_json2_body_refuses_what_it_cannot_name():
    with pytest.raises(OdooError, match="positional arguments"):
        _json2_body("hr.employee", "action_archive_something", [1], {})
    with pytest.raises(OdooError, match="Too many arguments"):
        _json2_body("hr.employee", "search_count", [[], 1, 2], {})
    with pytest.raises(OdooError, match="twice"):
        _json2_body("hr.employee", "search_read", [[]], {"domain": []})
    # Keyword-only calls to any method are fine.
    assert _json2_body("hr.employee", "action_x", [], {"context": {}}) == {"context": {}}
