"""tools/sqlite_to_postgres.py — the one-off move from SQLite into the Docker
stack's Postgres. The full copy runs only when TEST_POSTGRES_URL points at an
empty, throwaway database (it is emptied with --replace)."""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tools.sqlite_to_postgres import Rekey, rewrite_local


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:8100", "http://host.docker.internal:8100"),
    ("http://localhost:8069/odoo", "http://host.docker.internal:8069/odoo"),
    ("https://user:pw@localhost", "https://user:pw@host.docker.internal"),
    ("http://10.0.0.5:8007", "http://10.0.0.5:8007"),
    ("https://biobridge.easyinstance.com", "https://biobridge.easyinstance.com"),
    ("adms://CKJG201760123", "adms://CKJG201760123"),
    (None, None),
])
def test_rewrite_local(url, expected):
    assert rewrite_local(url) == expected


def test_rekey_moves_secrets_to_the_new_master_key(monkeypatch):
    from app.core import crypto

    monkeypatch.setattr(crypto.settings, "master_encryption_key", "old-key-old-key-old-key")
    crypto._CACHE.clear()
    old_cipher = crypto.encrypt("odoo-api-key", "tenant-1")
    old_mail = crypto.encrypt("smtp-pass", "platform:mail")

    monkeypatch.setattr(crypto.settings, "master_encryption_key", "new-key-new-key-new-key")
    crypto._CACHE.clear()
    rekey = Rekey("old-key-old-key-old-key")
    row = {"tenant_id": "tenant-1", "api_key_enc": old_cipher}
    rekey.row("odoo_connection", row)
    assert crypto.decrypt(row["api_key_enc"], "tenant-1") == "odoo-api-key"
    setting = {"key": "mail", "value": f'{{"host": "smtp.x", "password_enc": "{old_mail}"}}'}
    rekey.row("platform_setting", setting)
    import json
    assert crypto.decrypt(json.loads(setting["value"])["password_enc"], "platform:mail") == "smtp-pass"
    assert rekey.count == 2
    crypto._CACHE.clear()


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL")
def test_full_copy_into_postgres(tmp_path):
    from app.core.crypto import decrypt, encrypt
    from app.db import migrations
    from app.models import AttendanceRecord, DeviceSource, OdooConnection, Tenant
    from tools.sqlite_to_postgres import copy

    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    migrations.upgrade(engine)
    with Session(engine) as s:
        t = Tenant(name="Acme", slug="acme", timezone="Asia/Kolkata")
        s.add(t); s.flush()
        s.add(OdooConnection(tenant_id=t.id, name="Odoo", url="http://localhost:8069", db_name="d",
                             username="u", api_key_enc=encrypt("k", t.id)))
        s.add(DeviceSource(tenant_id=t.id, name="BT", provider="biotime", base_url="http://127.0.0.1:8100", username="u",
                           server_timezone="Asia/Kolkata", config={"record_cursor": 7}))
        s.add(AttendanceRecord(tenant_id=t.id, emp_code="1", check_in=datetime(2026, 10, 19, 6, 34, 26),
                               shift_date="2026-10-19", odoo_attendance_id=113))
        s.commit()
    engine.dispose()

    url = os.environ["TEST_POSTGRES_URL"]
    copied = copy(str(path), url, replace=True, rewrite_localhost=True, log=lambda *_: None)
    assert copied["tenant"] == 1 and copied["attendance_record"] == 1

    pg = create_engine(url)
    with Session(pg) as s:
        tenant = s.scalars(select(Tenant)).one()
        conn = s.scalars(select(OdooConnection)).one()
        assert conn.url == "http://host.docker.internal:8069"
        assert decrypt(conn.api_key_enc, tenant.id) == "k"
        assert s.scalars(select(DeviceSource)).one().config == {"record_cursor": 7}
        rec = s.scalars(select(AttendanceRecord)).one()
        assert rec.check_in.replace(tzinfo=None) == datetime(2026, 10, 19, 6, 34, 26)
    pg.dispose()
    with pytest.raises(SystemExit, match="already has data"):
        copy(str(path), url, log=lambda *_: None)
