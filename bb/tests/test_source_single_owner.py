"""One biometric server or device can be connected under one account only."""

from __future__ import annotations

from app.db.session import get_db
from app.main import app
from app.models import DeviceSource
from tests.test_api_isolation import auth, client, signup  # noqa: F401


def _tid(client, token):
    return client.get("/api/v1/tenant", headers=auth(token)).json()["id"]


def _add(client, token, url, provider="biotime", username="bot"):
    db = next(app.dependency_overrides[get_db]())
    db.add(DeviceSource(tenant_id=_tid(client, token), name="S", provider=provider,
                        base_url=url, username=username))
    db.commit()


def _test(client, token, url, provider="biotime", username="bot"):
    return client.post("/api/v1/sources/test", headers=auth(token),
                       json={"provider": provider, "base_url": url, "username": username,
                             "password": "x"})


def test_a_public_server_cannot_be_connected_by_two_accounts(client):
    a = signup(client, "A Co", "a@example.com")
    b = signup(client, "B Co", "b@example.org")
    _add(client, a, "https://biotime.acme-example.com")
    r = _test(client, b, "https://BioTime.acme-example.com:443/")
    assert r.status_code == 409
    assert "another BioBridge account" in r.json()["detail"]
    assert "A Co" not in r.json()["detail"]


def test_private_lan_addresses_can_repeat_across_accounts(client):
    a = signup(client, "A Co", "a@example.com")
    b = signup(client, "B Co", "b@example.org")
    _add(client, a, "zk://192.168.1.201", provider="zkteco")
    assert _test(client, b, "zk://192.168.1.201", provider="zkteco").status_code != 409


def test_cloud_accounts_are_keyed_by_login(client):
    a = signup(client, "A Co", "a@example.com")
    b = signup(client, "B Co", "b@example.org")
    _add(client, a, "https://cloud.anviz-example.com", provider="crosschex", username="hr@acme.com")
    assert _test(client, b, "https://cloud.anviz-example.com", provider="crosschex",
                 username="hr@acme.com").status_code == 409
    assert _test(client, b, "https://cloud.anviz-example.com", provider="crosschex",
                 username="other@x.com").status_code != 409
