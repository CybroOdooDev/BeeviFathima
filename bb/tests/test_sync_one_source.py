"""Sync now for one connection — each standalone device's own button."""

from __future__ import annotations

from sqlalchemy import select

import app.services.sync_engine as engine_mod
from app.models import PunchRecord
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_mixed_sources import _add_standalone, _on
from tests.test_sync_engine import punch


def _scoped(db, tenant, odoo, rows_by_source, monkeypatch, only):
    fetched = []

    def provider_for(t, source):
        fetched.append(source.name)
        rows = rows_by_source.get(source.name)
        if isinstance(rows, Exception):
            class Down(FakeProvider):
                def fetch_punches(self, since=None, until=None):
                    raise rows
            return Down([])
        return FakeProvider(rows or [])

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", provider_for)
    run = engine_mod.SyncEngine(db, tenant, "device", only_source_id=only).run_cycle()
    return run, fetched


def test_syncing_one_device_reads_only_that_device(db, tenant, local_day, monkeypatch):
    zk = _add_standalone(db, tenant)
    odoo = FakeOdoo()
    run, fetched = _scoped(db, tenant, odoo, {
        "BioTime": [punch(1, "1001", local_day.replace(hour=8))],
        "Warehouse ZK": [_on("ZK-WH-1", punch(1, "1002", local_day.replace(hour=9)))],
    }, monkeypatch, zk.id)

    assert run.status == "success", run.error_message
    assert fetched == ["Warehouse ZK"]
    assert {p.emp_code for p in db.scalars(select(PunchRecord))} == {"1002"}
    assert run.triggered_by == "device"


def test_a_switched_off_device_does_not_count_against_the_account(db, tenant, monkeypatch):
    from app.integrations.base import ProviderError

    zk = _add_standalone(db, tenant)
    tenant.consecutive_failures = 2
    db.commit()
    run, _ = _scoped(db, tenant, FakeOdoo(), {
        "Warehouse ZK": ProviderError("Timed out connecting to 10.0.11.43:4370"),
    }, monkeypatch, zk.id)

    assert run.status == "failed" and "Timed out" in run.error_message
    db.refresh(tenant)
    assert tenant.consecutive_failures == 2, "a one-device sync must not grow the streak"


def test_the_route_runs_that_source_and_is_tenant_scoped(monkeypatch):
    import app.api.v1.connections as connections_mod
    from app.integrations.base import ConnectionInfo
    from tests.test_connection_test_before_save import _StubProvider
    from tests.test_odoo_company_id_api import auth, signup
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db.session import get_db
    from app.main import app
    from app.models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            token = signup(client, "Acme", "owner@acme.example.com")
            monkeypatch.setattr(connections_mod, "build_source_provider",
                                lambda *_: _StubProvider(ConnectionInfo(ok=True, message="ok")))
            src = client.post("/api/v1/sources", json={"name": "Gate", "provider": "zk_device",
                              "connection_kind": "device", "base_url": "zk://10.0.11.43"},
                              headers=auth(token)).json()
            monkeypatch.setattr(engine_mod, "build_source_provider", lambda *_: FakeProvider([]))
            r = client.post(f"/api/v1/sources/{src['id']}/sync", headers=auth(token))
            assert r.status_code == 200, r.text
            assert r.json()["triggered_by"] == "device"
            other = signup(client, "Other", "owner@other.example.com")
            assert client.post(f"/api/v1/sources/{src['id']}/sync", headers=auth(other)).status_code == 404
    finally:
        app.dependency_overrides.clear()
