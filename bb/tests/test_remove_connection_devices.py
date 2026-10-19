"""Removing a connection removes its terminals' device records from Odoo,
unless another connection of the tenant still has the same terminal (same
serial number or IP address). See connections._remove_terminals_from_odoo."""
from __future__ import annotations

from tests.test_standalone_device_registration import (  # noqa: F401  (fixtures + helpers)
    SLUG, RecordingOdoo, _FakeUnit, _add, _devices, _odoo_with_tracking, _reset,
    _token, client,
)
import pytest

import app.api.v1.connections as connections_mod
from app.integrations.base import _REGISTRY
from sqlalchemy import select

from app.integrations.odoo import OdooClient
from app.models import OdooDeviceRemoval
from app.services import device_removals


@pytest.fixture(autouse=True, scope="module")
def _registered():
    _REGISTRY[SLUG] = _FakeUnit   # the original module unregisters it after itself
    yield


def _setup(client, monkeypatch):
    headers = _token(client)
    _odoo_with_tracking(client, headers)
    odoo = RecordingOdoo()
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: odoo)
    return headers, odoo


def test_removing_the_connection_deletes_its_odoo_device(client, monkeypatch):
    headers, odoo = _setup(client, monkeypatch)
    source = _add(client, headers)
    assert "OIN7010066122100033" in odoo.devices
    assert client.delete(f"/api/v1/sources/{source['id']}", headers=headers).status_code == 204
    assert odoo.devices == {}
    assert _devices(client) == []


def test_terminal_kept_when_another_connection_has_the_same_serial(client, monkeypatch):
    headers, odoo = _setup(client, monkeypatch)
    first = _add(client, headers)
    _add(client, headers, name="Second", base_url="https://10.0.11.44.test")
    client.delete(f"/api/v1/sources/{first['id']}", headers=headers)
    assert "OIN7010066122100033" in odoo.devices
    assert not any(c.startswith("delete_device") for c in odoo.calls)


def test_terminal_kept_when_another_connection_has_the_same_ip(client, monkeypatch):
    headers, odoo = _setup(client, monkeypatch)
    first = _add(client, headers)
    _FakeUnit.serial = "OTHER-SERIAL"            # same IP (10.0.11.43), new serial
    _add(client, headers, name="Second", base_url="https://10.0.11.44.test")
    client.delete(f"/api/v1/sources/{first['id']}", headers=headers)
    assert "OIN7010066122100033" in odoo.devices


def test_odoo_down_still_removes_the_connection(client, monkeypatch):
    headers, _ = _setup(client, monkeypatch)
    source = _add(client, headers)

    def boom(t, c):
        raise connections_mod.OdooError("down")
    monkeypatch.setattr(connections_mod, "build_odoo_client", boom)
    assert client.delete(f"/api/v1/sources/{source['id']}", headers=headers).status_code == 204
    assert _devices(client) == []
    (row,) = _queue(client)
    assert row.serial_number == "OIN7010066122100033" and row.source_name == "Front door"


def _queue(client):
    db = client.SessionLocal()
    try:
        return db.scalars(select(OdooDeviceRemoval)).all()
    finally:
        db.close()


def _remove_while_odoo_down(client, monkeypatch):
    headers, odoo = _setup(client, monkeypatch)
    source = _add(client, headers)

    def boom(t, c):
        raise connections_mod.OdooError("down")
    monkeypatch.setattr(connections_mod, "build_odoo_client", boom)
    client.delete(f"/api/v1/sources/{source['id']}", headers=headers)
    return headers, odoo


def test_queued_removal_is_done_once_odoo_is_back(client, monkeypatch):
    _, odoo = _remove_while_odoo_down(client, monkeypatch)
    assert "OIN7010066122100033" in odoo.devices
    db = client.SessionLocal()
    tenant_id = db.scalars(select(OdooDeviceRemoval)).one().tenant_id
    assert device_removals.work_off(db, tenant_id, odoo) == (1, [])
    db.commit(); db.close()
    assert odoo.devices == {} and _queue(client) == []


def test_queued_removal_dropped_when_the_terminal_comes_back(client, monkeypatch):
    headers, odoo = _remove_while_odoo_down(client, monkeypatch)
    monkeypatch.setattr(connections_mod, "build_odoo_client", lambda t, c: odoo)
    _add(client, headers, name="Re-added")              # same terminal again
    db = client.SessionLocal()
    tenant_id = db.scalars(select(OdooDeviceRemoval)).one().tenant_id
    assert device_removals.work_off(db, tenant_id, odoo) == (0, [])
    db.commit(); db.close()
    assert "OIN7010066122100033" in odoo.devices and _queue(client) == []


def test_refused_removal_stays_queued_with_the_error(client, monkeypatch):
    _, odoo = _remove_while_odoo_down(client, monkeypatch)

    def refuse(serial, company_id=None):
        raise connections_mod.OdooError("Access denied")
    odoo.delete_device = refuse
    db = client.SessionLocal()
    tenant_id = db.scalars(select(OdooDeviceRemoval)).one().tenant_id
    removed, errors = device_removals.work_off(db, tenant_id, odoo)
    db.commit(); db.close()
    assert removed == 0 and len(errors) == 1
    (row,) = _queue(client)
    assert row.attempts == 1 and "Access denied" in row.last_error


class _Stub(OdooClient):
    def __init__(self, mode, rows):
        self._device_mode, self.rows, self.calls = mode, rows, []
    def fields_of(self, model): return {"x_company_id"}
    def company_scope(self): return None
    def execute(self, model, method, args, kwargs=None, **kw):
        self.calls.append((model, method, args))
        return self.rows if method == "search" else True


def test_client_deletes_by_serial_in_both_modes():
    bootstrap = _Stub("bootstrap", [5])
    assert bootstrap.delete_device("SN1", company_id=3)
    assert bootstrap.calls[0][0] == "x_biobridge_device"
    assert ("x_company_id", "=", 3) in bootstrap.calls[0][2][0]
    assert bootstrap.calls[1] == ("x_biobridge_device", "unlink", [[5]])
    module = _Stub("module", [9])
    assert module.delete_device("SN1", company_id=3)
    assert module.calls[0][0] == "biobridge.device"


def test_client_skips_ambiguous_or_missing():
    assert not _Stub("bootstrap", []).delete_device("SN1", company_id=3)
    assert not _Stub("bootstrap", [1, 2]).delete_device("SN1")      # which company's?
    assert _Stub("bootstrap", [1, 2]).delete_device("SN1", company_id=3)
    assert not _Stub(None, [1]).delete_device("SN1")
