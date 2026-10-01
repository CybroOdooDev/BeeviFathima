"""tools/generate_punches.py and tools/mock_biotime.py for any Odoo company:
the roster comes from Odoo by res.company id, is cached, and both tools agree
on the same people and terminals."""

from __future__ import annotations

import json
import socket
import sys
import threading

import httpx
import pytest

import tools.mock_biotime as mock_biotime
import tools.mock_roster as mock_roster


class FakeOdoo:
    def __init__(self):
        self.company_id = None

    def authenticate(self):
        return 7

    def list_companies(self):
        return [{"id": 1, "name": "Main Co"}, {"id": 7, "name": "Kochi Branch"}]

    def list_employees(self):
        return [
            {"id": 31, "name": "Anu Thomas", "active": True, "barcode": "7001", "pin": False,
             "department_id": [5, "Kochi / Production"]},
            {"id": 32, "name": "Rahul", "active": True, "barcode": False, "pin": "7002", "department_id": False},
            {"id": 33, "name": "No Badge Person", "active": True, "barcode": False, "pin": False},
            {"id": 34, "name": "Left Company", "active": False, "barcode": "7009"},
        ]


@pytest.fixture
def odoo(monkeypatch, tmp_path):
    fake = FakeOdoo()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mock_roster, "odoo_client", lambda args, company_id: fake)
    return fake


def test_generate_punches_for_an_odoo_company(odoo, tmp_path, monkeypatch, capsys):
    from tools.generate_punches import main

    monkeypatch.setattr(sys, "argv", ["generate_punches.py", "--company", "7", "--days", "3",
                                      "--include-weekends", "--seed", "1"])
    assert main() == 0
    rows = json.loads((tmp_path / "punches_company7.json").read_text())
    assert {r["emp_code"] for r in rows} == {"7001", "7002"}
    assert {r["terminal_sn"] for r in rows} <= {"MOCK-C7-GATE-01", "MOCK-C7-GATE-02"}
    roster = json.loads((tmp_path / "roster_company7.json").read_text())
    assert roster["company_name"] == "Kochi Branch" and roster["skipped"] == ["No Badge Person"]
    assert roster["employees"][0]["department"]["dept_name"] == "Production"
    assert "Kochi Branch" in capsys.readouterr().out


def test_unknown_company_lists_the_ones_that_exist(odoo):
    args = type("A", (), {"refresh_roster": False})()
    with pytest.raises(SystemExit, match="Kochi Branch"):
        mock_roster.load_roster(args, 99)


def test_mock_server_serves_the_cached_company(odoo, tmp_path, monkeypatch):
    args = type("A", (), {"refresh_roster": False})()
    roster = mock_roster.load_roster(args, 7)            # reads Odoo, writes the cache
    monkeypatch.setattr(mock_roster, "odoo_client", lambda *a: pytest.fail("cache should be used"))
    cached = mock_roster.load_roster(args, 7)
    assert cached.source == "cache" and cached.emp_codes == roster.emp_codes == ["7001", "7002"]

    monkeypatch.setattr(mock_biotime, "EMPLOYEES", cached.employees)
    monkeypatch.setattr(mock_biotime, "TERMINALS", cached.terminals)
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    server = mock_biotime.build_server("127.0.0.1", port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        head = {"Authorization": f"Token {mock_biotime.TOKEN}"}
        people = httpx.get(f"http://127.0.0.1:{port}/personnel/api/employees/?page_size=10", headers=head).json()
        terms = httpx.get(f"http://127.0.0.1:{port}/iclock/api/terminals/", headers=head).json()
    finally:
        server.shutdown()
    assert [e["emp_code"] for e in people["data"]] == ["7001", "7002"]
    assert {t["sn"] for t in terms["data"]} == {"MOCK-C7-GATE-01", "MOCK-C7-GATE-02"}


def test_without_odoo_the_old_fixtures_still_work(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mock_roster, "odoo_client", lambda *a: None)
    args = type("A", (), {"refresh_roster": False})()
    assert mock_roster.load_roster(args, 2).emp_codes == ["2001", "2002", "2003"]
    with pytest.raises(SystemExit, match="Connect Odoo"):
        mock_roster.load_roster(args, 9)
