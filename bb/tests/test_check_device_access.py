"""tools/check_device_access.py names the cause of a BioBridge Device access error."""

from __future__ import annotations

import sys

from sqlalchemy import select

import tools.check_device_access as tool
from app.models import OdooConnection

OLD = "[('x_company_id', 'in', company_ids)]"


class FakeOdoo:
    def __init__(self, rule_domain=tool.CURRENT_DOMAIN, devices=None, extra_rules=()):
        self.rules = [{"name": tool.RULE_NAME, "domain_force": rule_domain, "active": True,
                       "groups": [], "perm_read": True, "perm_write": True,
                       "perm_create": True, "perm_unlink": True}, *extra_rules]
        self.devices = devices if devices is not None else [
            {"id": 1, "x_name": "Main Gate", "x_serial_number": "GATE-01", "x_company_id": [1, "Co 1"]},
        ]
        self.writes = []

    def authenticate(self):
        return 2

    def execute(self, model, method, args, kwargs=None, *, scope_to_company=True):
        if method not in ("read", "search", "search_read"):
            self.writes.append((model, method))
            raise AssertionError("the tool must never write")
        if model == "res.users":
            return [{"name": "Administrator", "company_id": [1, "Co 1"], "company_ids": [1, 2, 4]}]
        if model == "ir.model":
            return [99]
        if model == "ir.rule":
            return self.rules
        if model == "x_biobridge_device":
            return self.devices
        raise AssertionError(f"unexpected {model}.{method}")


def _run(db, tenant, monkeypatch, capsys, fake, company_id=4):
    conn = db.scalar(select(OdooConnection))
    conn.company_id = company_id
    conn.has_device_tracking = True
    conn.device_tracking_mode = "bootstrap"
    db.commit()
    monkeypatch.setattr(tool, "SessionLocal", lambda: db)
    monkeypatch.setattr(tool, "build_odoo_client", lambda t, c: fake)
    monkeypatch.setattr(sys, "argv", ["check_device_access.py"])
    code = tool.main()
    return code, capsys.readouterr().out


def test_flags_the_old_strict_rule_domain(db, tenant, monkeypatch, capsys):
    code, out = _run(db, tenant, monkeypatch, capsys, FakeOdoo(rule_domain=OLD))
    assert code == 1 and "still has an old domain" in out


def test_flags_a_terminal_whose_odoo_device_belongs_to_another_company(db, tenant, monkeypatch, capsys):
    # GATE-01 is this account's terminal; its Odoo device is company 1, connection pinned to 4.
    code, out = _run(db, tenant, monkeypatch, capsys, FakeOdoo())
    assert code == 1
    assert "belongs to company 1 while the connection is pinned to 4" in out


def test_flags_an_extra_rule_biobridge_did_not_make(db, tenant, monkeypatch, capsys):
    extra = {"name": "studio company", "domain_force": "[('x_studio_company', 'in', company_ids)]",
             "active": True, "groups": [], "perm_read": True, "perm_write": True,
             "perm_create": True, "perm_unlink": True}
    code, out = _run(db, tenant, monkeypatch, capsys, FakeOdoo(devices=[], extra_rules=[extra]))
    assert "Extra rule 'studio company'" in out


def test_clean_setup_reports_nothing_wrong(db, tenant, monkeypatch, capsys):
    fake = FakeOdoo(devices=[
        {"id": 5, "x_name": "Gate", "x_serial_number": "GATE-01", "x_company_id": [4, "Co 4"]},
    ])
    code, out = _run(db, tenant, monkeypatch, capsys, fake)
    assert code == 0 and "nothing wrong found here" in out
    assert fake.writes == []
