"""Each provider is offered, and accepted, only under the kinds it declares —
BioTime is a server, so it is a platform connection and never a device."""

from __future__ import annotations

from sqlalchemy import select

from app.core.crypto import encrypt
from app.integrations.base import available_providers
from app.models import DeviceSource, Tenant
from tests.test_platform_admin import api, head, signup  # noqa: F401


SHIPPED = {"biotime", "zk_device", "zk_adms", "hik_isapi", "biostar2", "cosec", "cosec_centra", "crosschex", "hikconnect", "hikcentral", "cams", "dahua"}


def test_every_shipped_provider_is_one_kind_and_biotime_is_a_platform():
    kinds = {p["slug"]: p["kinds"] for p in available_providers() if p["slug"] in SHIPPED}
    assert set(kinds) == SHIPPED, "test doubles registered by other tests are left out"
    assert kinds["biotime"] == ["platform"]
    assert all(len(k) == 1 for k in kinds.values()), kinds
    devices = sorted(s for s, k in kinds.items() if k == ["device"])
    assert devices == ["cams", "cosec", "dahua", "hik_isapi", "zk_adms", "zk_device"]


def test_biotime_cannot_be_added_as_a_standalone_device(api):
    token = signup(api, "Acme", "owner@acme.example.com")
    r = api.post("/api/v1/sources", headers=head(token), json={
        "name": "BT", "provider": "biotime", "connection_kind": "device",
        "base_url": "https://bt.example.com", "username": "admin", "password": "pw"})
    assert r.status_code == 400 and "not offered as a" in r.json()["detail"]


def test_the_fix_tool_refiles_an_old_biotime_device(api, capsys, monkeypatch):
    from tools import fix_connection_kinds as tool

    signup(api, "Acme", "owner@acme.example.com")
    s = api.session_factory()
    tenant = s.scalars(select(Tenant)).first()
    s.add(DeviceSource(tenant_id=tenant.id, name="Old BT", provider="biotime", connection_kind="device",
                       base_url="https://bt.example.com", username="admin",
                       password_enc=encrypt("pw", tenant.crypto_key), server_timezone="UTC", is_active=True))
    s.commit(); s.close()

    monkeypatch.setattr(tool, "SessionLocal", api.session_factory)
    assert tool.main([]) == 0
    assert "-> platform" in capsys.readouterr().out
    s = api.session_factory()
    assert s.scalars(select(DeviceSource)).one().connection_kind == "device", "report only changes nothing"
    s.close()

    assert tool.main(["--apply"]) == 0
    s = api.session_factory()
    assert s.scalars(select(DeviceSource)).one().connection_kind == "platform"
    s.close()
    assert tool.main([]) == 0
    assert "nothing to fix" in capsys.readouterr().out
