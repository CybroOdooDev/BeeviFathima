"""The mock BioTime tools are tenant-scoped: --tenant is mandatory, a run covers
the companies enabled for that tenant, and one tenant's employees, terminals and
punches are never visible to another."""

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
    """Two companies, each with its own people."""

    def __init__(self, company_id=None):
        self.company_id = company_id

    def authenticate(self):
        return 7

    def list_companies(self):
        return [{"id": 1, "name": "Main Co"}, {"id": 7, "name": "Kochi Branch"}]

    def list_employees(self):
        people = {
            1: [{"id": 11, "name": "Asha Rao", "active": True, "barcode": "1001"}],
            7: [{"id": 31, "name": "Anu Thomas", "active": True, "barcode": "7001"},
                {"id": 32, "name": "Rahul Nair", "active": True, "barcode": "7002"}],
        }
        return people[self.company_id]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mock_roster, "odoo_client", lambda args, company_id: FakeOdoo(company_id))
    monkeypatch.setattr(mock_roster, "odoo_login", lambda args: ("http://odoo.test", "db", "u", "k"))
    settings = {"known": None, "pin": None, "disabled": []}
    monkeypatch.setattr(mock_roster, "_tenant_settings", lambda tenant: dict(settings))
    return settings


def args(**kw):
    base = {"tenant": "acme", "company": None, "refresh_roster": False, "data_dir": "mock_data"}
    base.update(kw)
    return type("A", (), base)()


def test_tenant_is_mandatory(env):
    with pytest.raises(SystemExit, match="--tenant is required"):
        mock_roster.resolve_scope(args(tenant=None))
    with pytest.raises(SystemExit, match="--tenant is required"):
        mock_roster.resolve_scope(args(tenant="  "))


def test_unknown_tenant_is_refused_when_biobridge_can_be_read(env, monkeypatch):
    monkeypatch.setattr(mock_roster, "_tenant_settings",
                        lambda tenant: {"known": False, "slugs": ["acme"], "pin": None, "disabled": []})
    with pytest.raises(SystemExit, match="no account with the slug"):
        mock_roster.resolve_scope(args(tenant="nobody"))


def test_no_company_arg_means_every_enabled_company(env):
    scope = mock_roster.resolve_scope(args())
    assert [c["id"] for c in scope.companies] == [1, 7]


def test_switched_off_companies_are_left_out(env):
    env["disabled"] = [1]
    assert [c["id"] for c in mock_roster.resolve_scope(args()).companies] == [7]


def test_a_pinned_connection_covers_only_its_company(env):
    env["pin"] = 7
    assert [c["id"] for c in mock_roster.resolve_scope(args()).companies] == [7]


def test_a_disabled_company_cannot_be_asked_for(env):
    env["disabled"] = [1]
    with pytest.raises(SystemExit, match="switched off"):
        mock_roster.resolve_scope(args(company=1))


def test_every_company_switched_off_is_an_error(env):
    env["disabled"] = [1, 7]
    with pytest.raises(SystemExit, match="no enabled company"):
        mock_roster.resolve_scope(args())


def test_company_arg_narrows_to_one_enabled_company(env):
    assert [c["id"] for c in mock_roster.resolve_scope(args(company=7)).companies] == [7]


def test_tenant_roster_spans_companies_with_tenant_scoped_terminals(env):
    scope = mock_roster.resolve_scope(args())
    roster = mock_roster.load_tenant_roster(args(), scope)
    assert roster.emp_codes == ["1001", "7001", "7002"]
    assert {t["sn"] for t in roster.terminals} == {
        "MOCK-ACME-C1-GATE-01", "MOCK-ACME-C1-GATE-02", "MOCK-ACME-C7-GATE-01", "MOCK-ACME-C7-GATE-02"}
    by_code = {e["emp_code"]: e["_company_id"] for e in roster.employees}
    assert by_code == {"1001": 1, "7001": 7, "7002": 7}


def test_a_badge_used_in_two_companies_is_served_once(env, monkeypatch):
    class Clash(FakeOdoo):
        def list_employees(self):
            return [{"id": 1, "name": "Same Badge", "active": True, "barcode": "5000"}]
    monkeypatch.setattr(mock_roster, "odoo_client", lambda a, cid: Clash(cid))
    roster = mock_roster.load_tenant_roster(args(), mock_roster.resolve_scope(args()))
    assert roster.emp_codes == ["5000"]
    assert any("already used in company 1" in s for s in roster.skipped)


def test_punches_use_each_employees_own_company_terminals(env, monkeypatch):
    from tools.generate_punches import main

    monkeypatch.setattr(sys, "argv", ["generate_punches.py", "--tenant", "acme", "--days", "3",
                                      "--include-weekends", "--seed", "1"])
    assert main() == 0
    rows = json.loads((mock_roster.Path("mock_data") / "acme" / "punches.json").read_text())
    for row in rows:
        want = "C1" if row["emp_code"] == "1001" else "C7"
        assert f"-{want}-GATE-" in row["terminal_sn"], row


def test_tenants_have_separate_folders_and_serials(env, monkeypatch):
    first = mock_roster.resolve_scope(args(tenant="acme"))
    second = mock_roster.resolve_scope(args(tenant="globex"))
    assert first.data_dir != second.data_dir
    a = mock_roster.load_tenant_roster(args(tenant="acme"), first)
    b = mock_roster.load_tenant_roster(args(tenant="globex"), second)
    assert not ({t["sn"] for t in a.terminals} & {t["sn"] for t in b.terminals})


def test_a_folder_copied_from_another_tenant_is_refused(env):
    scope = mock_roster.resolve_scope(args(tenant="acme"))
    mock_roster.load_tenant_roster(args(tenant="acme"), scope)
    (mock_roster.Path("mock_data") / "globex").mkdir()
    for f in (mock_roster.Path("mock_data") / "acme").iterdir():
        (mock_roster.Path("mock_data") / "globex" / f.name).write_text(f.read_text())
    with pytest.raises(SystemExit, match="refusing to mix tenants"):
        mock_roster.resolve_scope(args(tenant="globex"))


def test_another_tenants_serials_are_refused():
    with pytest.raises(SystemExit, match="belongs to tenant ACME"):
        mock_roster.scope_serials([{"sn": "MOCK-ACME-C1-GATE-01"}], "globex")


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def served(env, monkeypatch, tmp_path):
    scope = mock_roster.resolve_scope(args())
    roster = mock_roster.load_tenant_roster(args(), scope)
    monkeypatch.setattr(mock_biotime, "TENANT", "acme")
    monkeypatch.setattr(mock_biotime, "EMPLOYEES", roster.employees)
    monkeypatch.setattr(mock_biotime, "DEPARTMENTS", roster.departments)
    monkeypatch.setattr(mock_biotime, "TERMINALS", roster.terminals)
    server = mock_biotime.build_server("127.0.0.1", _free_port())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_only_the_tenants_own_login_is_accepted(served):
    ok = httpx.post(f"{served}/api-token-auth/", json={"username": "acme", "password": "x"})
    assert ok.status_code == 200 and ok.json()["token"] == mock_biotime.TOKEN
    assert ok.headers["X-Mock-Tenant"] == "acme"
    other = httpx.post(f"{served}/api-token-auth/", json={"username": "globex", "password": "x"})
    assert other.status_code == 400 and "belongs to tenant acme" in other.text


def test_bookkeeping_never_leaks_onto_the_wire(served):
    headers = {"Authorization": f"Token {mock_biotime.TOKEN}"}
    for path in ("/personnel/api/employees/", "/iclock/api/terminals/"):
        rows = httpx.get(f"{served}{path}", headers=headers).json()["data"]
        assert rows and not any(k.startswith("_") for r in rows for k in r)


def test_manual_punch_uses_the_employees_company_terminals(served, tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    path.write_text("[]")
    monkeypatch.setattr(mock_biotime, "PUNCH_FILE", str(path))
    row = httpx.post(f"{served}/mock/punch", json={"emp_code": "7001"}).json()
    assert "-C7-GATE-" in row["terminal_sn"]
    row = httpx.post(f"{served}/mock/punch", json={"emp_code": "1001"}).json()
    assert "-C1-GATE-" in row["terminal_sn"]


def test_live_punches_are_on_by_default(env, monkeypatch, tmp_path):
    """No generate_punches step: starting the mock is enough to get new punches,
    each on the employee's own company's terminal."""
    import time

    monkeypatch.setattr(sys, "argv", ["mock_biotime.py", "--tenant", "acme", "--interval", "0.05",
                                      "--port", str(_free_port())])
    # main() sets module globals; let monkeypatch put them back afterwards.
    for name in ("TENANT", "EMPLOYEES", "DEPARTMENTS", "TERMINALS", "PUNCH_FILE", "SERVER_TZ"):
        monkeypatch.setattr(mock_biotime, name, getattr(mock_biotime, name))
    real = mock_biotime.simulate

    def short(interval, stop, burst=1):
        threading.Timer(0.4, stop.set).start()   # the thread must not outlive the test
        real(interval, stop, burst)

    monkeypatch.setattr(mock_biotime, "simulate", short)
    started = {}
    monkeypatch.setattr(mock_biotime.ThreadingHTTPServer, "serve_forever",
                        lambda self, *a, **k: started.setdefault("ok", True))
    mock_biotime.main()
    time.sleep(0.7)
    rows = json.loads((tmp_path / "mock_data" / "acme" / "punches.json").read_text())
    assert rows, "the live simulator wrote nothing"
    for row in rows:
        want = "C1" if row["emp_code"] == "1001" else "C7"
        assert f"-{want}-GATE-" in row["terminal_sn"]
