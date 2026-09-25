"""Test Connection on a form that has not been saved yet.

The connection forms used to offer Test Connection only after Connect had
already stored the credentials, so the first time anyone learned a URL was
wrong was after committing it. These pin the pre-save probes: they report
what the probe found, they use the stored secret when the edit form leaves
it blank, and — the property that matters most — they write nothing.
"""

from __future__ import annotations

from sqlalchemy import select

import app.api.v1.connections as connections_mod
from app.core.crypto import decrypt
from app.integrations.base import ConnectionInfo as ProviderTestResult, ProviderError
from app.integrations.odoo import OdooError
from app.models import DeviceSource, OdooConnection, Tenant
from tests.test_odoo_company_id_api import _StubOdooClient, auth, client, signup  # noqa: F401

PING_OK = {
    "uid": 7,
    "server_version": "17.0",
    "employee_count": 3,
    "can_create_attendance": True,
    "companies": [{"id": 1, "name": "Acme"}],
}


def _session(client):  # noqa: F811
    from app.db.session import get_db
    from app.main import app

    return next(app.dependency_overrides[get_db]())


# ===========================================================================
# Odoo
# ===========================================================================
def test_an_unsaved_odoo_form_can_be_tested(client, monkeypatch):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    monkeypatch.setattr(
        connections_mod, "build_odoo_client", lambda *_: _StubOdooClient(PING_OK)
    )
    response = client.post(
        "/api/v1/odoo-connections/test",
        json={"url": "https://acme.odoo.com", "db_name": "acme",
              "username": "api@acme.com", "api_key": "k"},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and "17.0" in body["message"]
    assert body["detail"]["companies"] == [{"id": 1, "name": "Acme"}]


def test_testing_an_unsaved_odoo_form_saves_nothing(client, monkeypatch):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    monkeypatch.setattr(
        connections_mod, "build_odoo_client", lambda *_: _StubOdooClient(PING_OK)
    )
    client.post(
        "/api/v1/odoo-connections/test",
        json={"url": "https://acme.odoo.com", "db_name": "acme",
              "username": "api@acme.com", "api_key": "k"},
        headers=auth(token),
    )
    assert client.get("/api/v1/odoo-connections", headers=auth(token)).json() == []


def test_a_failing_unsaved_odoo_form_reports_why(client, monkeypatch):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    monkeypatch.setattr(
        connections_mod, "build_odoo_client",
        lambda *_: _StubOdooClient(ping_error=OdooError("Access denied")),
    )
    body = client.post(
        "/api/v1/odoo-connections/test",
        json={"url": "https://acme.odoo.com", "db_name": "acme",
              "username": "api@acme.com", "api_key": "wrong"},
        headers=auth(token),
    ).json()
    assert body == {"ok": False, "message": "Access denied", "detail": {}}


def test_an_unsaved_odoo_form_with_no_key_is_refused(client):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    response = client.post(
        "/api/v1/odoo-connections/test",
        json={"url": "https://acme.odoo.com", "db_name": "acme", "username": "api@acme.com"},
        headers=auth(token),
    )
    assert response.status_code == 400
    assert "API key" in response.json()["detail"]


def test_the_edit_form_tests_with_the_stored_key_and_leaves_the_row_alone(
    client, monkeypatch  # noqa: F811
):
    token = signup(client, "Acme", "owner@acme.example.com")
    seen = {}

    def build(tenant, conn):
        seen["key"] = decrypt(conn.api_key_enc, tenant.crypto_key)
        seen["url"] = conn.url
        return _StubOdooClient(PING_OK)

    monkeypatch.setattr(connections_mod, "build_odoo_client", build)
    saved = client.post(
        "/api/v1/odoo-connections",
        json={"url": "https://acme.odoo.com", "db_name": "acme",
              "username": "api@acme.com", "api_key": "the-stored-key"},
        headers=auth(token),
    ).json()

    # Now the probe will fail — so if the edit-form test touched the stored
    # row, its status would flip to failed.
    monkeypatch.setattr(
        connections_mod, "build_odoo_client",
        lambda tenant, conn: (
            seen.update(key=decrypt(conn.api_key_enc, tenant.crypto_key), url=conn.url)
            or _StubOdooClient(ping_error=OdooError("Host not found"))
        ),
    )
    body = client.post(
        "/api/v1/odoo-connections/test",
        json={"url": "https://typo.odoo.com", "db_name": "acme",
              "username": "api@acme.com", "conn_id": saved["id"]},
        headers=auth(token),
    ).json()
    assert body["ok"] is False
    assert seen == {"key": "the-stored-key", "url": "https://typo.odoo.com"}

    (row,) = client.get("/api/v1/odoo-connections", headers=auth(token)).json()
    assert row["url"] == "https://acme.odoo.com"
    assert row["status"] == "connected", "a pre-save test must not touch the stored row"


def test_another_accounts_connection_cannot_lend_its_key(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        connections_mod, "build_odoo_client", lambda *_: _StubOdooClient(PING_OK)
    )
    owner = signup(client, "Acme", "owner@acme.example.com")
    saved = client.post(
        "/api/v1/odoo-connections",
        json={"url": "https://acme.odoo.com", "db_name": "acme",
              "username": "api@acme.com", "api_key": "k"},
        headers=auth(owner),
    ).json()

    other = signup(client, "Other", "owner@other.example.com")
    response = client.post(
        "/api/v1/odoo-connections/test",
        json={"url": "https://attacker.example.com", "db_name": "x",
              "username": "x", "conn_id": saved["id"]},
        headers=auth(other),
    )
    assert response.status_code == 404


# ===========================================================================
# Biometric sources
# ===========================================================================
class _StubProvider:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.cached_token = "minted-during-the-test"

    def test_connection(self):
        if self._error:
            raise self._error
        return self._result

    def close(self):
        pass


def test_an_unsaved_device_form_can_be_tested(client, monkeypatch):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    seen = {}

    def build(tenant, source):
        seen["provider"] = source.provider
        seen["address"] = source.base_url
        return _StubProvider(ProviderTestResult(ok=True, message="Connected to ZK device"))

    monkeypatch.setattr(connections_mod, "build_source_provider", build)
    response = client.post(
        "/api/v1/sources/test",
        json={"provider": "zk_device", "base_url": "zk://10.0.11.43",
              "server_timezone": "Asia/Kolkata"},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert seen == {"provider": "zk_device", "address": "zk://10.0.11.43"}


def test_a_timed_out_device_is_reported_and_nothing_is_saved(client, monkeypatch):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    monkeypatch.setattr(
        connections_mod, "build_source_provider",
        lambda *_: _StubProvider(error=ProviderError("Timed out connecting to 10.0.11.43:4370")),
    )
    body = client.post(
        "/api/v1/sources/test",
        json={"provider": "zk_device", "base_url": "zk://10.0.11.43"},
        headers=auth(token),
    ).json()
    assert body["ok"] is False and "Timed out" in body["message"]
    assert client.get("/api/v1/sources", headers=auth(token)).json() == []


def test_a_platform_form_still_needs_its_required_fields(client):  # noqa: F811
    token = signup(client, "Acme", "owner@acme.example.com")
    response = client.post(
        "/api/v1/sources/test",
        json={"provider": "biotime", "base_url": "https://biotime.example.com"},
        headers=auth(token),
    )
    assert response.status_code == 400
    assert "required" in response.json()["detail"]


def test_the_edit_form_uses_the_stored_password_and_mints_no_stored_token(
    client, monkeypatch  # noqa: F811
):
    token = signup(client, "Acme", "owner@acme.example.com")
    monkeypatch.setattr(
        connections_mod, "build_source_provider",
        lambda *_: _StubProvider(ProviderTestResult(ok=True, message="ok")),
    )
    saved = client.post(
        "/api/v1/sources",
        json={"name": "Main", "provider": "biotime", "base_url": "https://bt.example.com",
              "username": "admin", "password": "stored-password"},
        headers=auth(token),
    ).json()

    seen = {}

    def build(tenant, source):
        seen["password"] = decrypt(source.password_enc, tenant.crypto_key)
        stub = _StubProvider(ProviderTestResult(ok=True, message="ok"))
        stub.cached_token = "minted-by-the-pre-save-test"
        return stub

    monkeypatch.setattr(connections_mod, "build_source_provider", build)
    response = client.post(
        "/api/v1/sources/test",
        json={"base_url": "https://bt2.example.com", "username": "admin",
              "source_id": saved["id"]},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    assert seen["password"] == "stored-password"

    db = _session(client)
    row = db.scalars(select(DeviceSource)).one()
    assert row.base_url == "https://bt.example.com", "the stored row is untouched"
    tenant = db.get(Tenant, row.tenant_id)
    # The create call's own probe cached a token; the pre-save test's must not
    # have replaced it — it never reaches the session at all.
    assert decrypt(row.token_enc, tenant.crypto_key) == "minted-during-the-test"
    assert db.scalars(select(OdooConnection)).all() == []
    db.close()


# ===========================================================================
# One "+ Add connection", either kind
# ===========================================================================
def test_platform_servers_and_standalone_devices_can_sit_side_by_side(
    client, monkeypatch  # noqa: F811
):
    """The kind is picked per connection now, not once per account."""
    token = signup(client, "Acme", "owner@acme.example.com")
    monkeypatch.setattr(
        connections_mod, "build_source_provider",
        lambda *_: _StubProvider(ProviderTestResult(ok=True, message="ok")),
    )
    platform = client.post(
        "/api/v1/sources",
        json={"name": "HQ BioTime", "provider": "biotime", "connection_kind": "platform",
              "base_url": "https://bt.example.com", "username": "admin", "password": "pw"},
        headers=auth(token),
    )
    device = client.post(
        "/api/v1/sources",
        json={"name": "Gate terminal", "provider": "zk_device", "connection_kind": "device",
              "base_url": "zk://10.0.11.43"},
        headers=auth(token),
    )
    assert platform.status_code == 201, platform.text
    assert device.status_code == 201, device.text
    kinds = sorted(s["connection_kind"] for s in client.get("/api/v1/sources", headers=auth(token)).json())
    assert kinds == ["device", "platform"]
