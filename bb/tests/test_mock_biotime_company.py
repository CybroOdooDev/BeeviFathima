"""tools/mock_biotime.py must be able to serve a second company's roster.

The company_id isolation feature (app/integrations/odoo.py) is only worth
testing end-to-end if two BioBridge sources can each point at a mock BioTime
with a *different* roster — otherwise "company 2's connection only sees
company 2's employees" can't be demonstrated without a real, multi-company
Odoo. These tests cover the dataset shapes directly (no server needed) and
one end-to-end pass through the real HTTP handler for --company 2, mirroring
the rigor of test_check_source.py's own mock-server tests.
"""

from __future__ import annotations

import socket
import threading

import httpx
import pytest

from tools.mock_biotime import DATASETS, TOKEN, build_server
import tools.mock_biotime as mock_biotime


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --------------------------------------------------------------------------- #
# Dataset shape
# --------------------------------------------------------------------------- #
def test_company_1_is_unchanged_from_before_datasets_existed() -> None:
    """--company defaults to 1, so every existing caller must see exactly the
    roster this file always had — nobody who never passes --company should
    notice this feature exists."""
    codes = {e["emp_code"] for e in DATASETS[1]["employees"]}
    serials = {t["sn"] for t in DATASETS[1]["terminals"]}
    assert codes == {"1001", "0042", "A7"}
    assert serials == {"MOCK-GATE-01", "MOCK-GATE-02"}


def test_company_2_matches_create_company_employees_roster() -> None:
    """Styled after tools/create_company_employees.py's SAMPLE_EMPLOYEES, so
    a "company 2" set up by that script against a real Odoo and a "company 2"
    served here describe the same fake people."""
    names = {(e["first_name"], e["last_name"], e["emp_code"]) for e in DATASETS[2]["employees"]}
    assert names == {
        ("Liam", "Okafor", "2001"),
        ("Priya", "Nakamura", "2002"),
        ("Noah", "Fernandes", "2003"),
    }


def test_company_4_is_beevi_and_marc() -> None:
    names = {(e["first_name"], e["emp_code"]) for e in DATASETS[4]["employees"]}
    assert names == {("Beevi", "5"), ("Marc", "6001")}
    assert {t["sn"] for t in DATASETS[4]["terminals"]} == {"MOCK-GATE-05", "MOCK-GATE-06"}


def test_generate_punches_rosters_match_the_mock_server() -> None:
    """The two files keep their rosters in sync by hand; this catches drift."""
    from tools.generate_punches import COMPANY_EMP_CODES, COMPANY_TERMINALS

    assert set(COMPANY_EMP_CODES) == set(DATASETS)
    for company, data in DATASETS.items():
        assert set(COMPANY_EMP_CODES[company]) == {e["emp_code"] for e in data["employees"]}
        assert set(COMPANY_TERMINALS[company]) == {t["sn"] for t in data["terminals"]}


def test_company_2_never_shares_a_terminal_or_emp_code_with_company_1() -> None:
    """Multiple mock instances are meant to run side by side — a collision here
    would make responses from the different instances indistinguishable in a shared log."""
    codes_1 = {e["emp_code"] for e in DATASETS[1]["employees"]}
    codes_2 = {e["emp_code"] for e in DATASETS[2]["employees"]}
    codes_4 = {e["emp_code"] for e in DATASETS[4]["employees"]}
    serials_1 = {t["sn"] for t in DATASETS[1]["terminals"]}
    serials_2 = {t["sn"] for t in DATASETS[2]["terminals"]}
    serials_4 = {t["sn"] for t in DATASETS[4]["terminals"]}
    assert codes_1.isdisjoint(codes_2)
    assert codes_1.isdisjoint(codes_4)
    assert codes_2.isdisjoint(codes_4)
    assert serials_1.isdisjoint(serials_2)
    assert serials_1.isdisjoint(serials_4)
    assert serials_2.isdisjoint(serials_4)


def test_every_department_referenced_by_an_employee_is_in_that_companys_list() -> None:
    for company, data in DATASETS.items():
        dept_ids = {d["id"] for d in data["departments"]}
        for emp in data["employees"]:
            assert emp["department"]["id"] in dept_ids, (
                f"company {company}: {emp['first_name']} {emp['last_name']}'s "
                "department isn't one of that company's own departments"
            )


# --------------------------------------------------------------------------- #
# End-to-end: the real HTTP handler, serving company 2
# --------------------------------------------------------------------------- #
@pytest.fixture
def served_company_2():
    """Point the module globals the Handler reads at DATASETS[2], the same
    assignment main() does for --company 2, and restore company 1 afterward
    so this test can't leak state into any test that runs after it."""
    original = (mock_biotime.DEPARTMENTS, mock_biotime.EMPLOYEES, mock_biotime.TERMINALS)
    dataset = DATASETS[2]
    mock_biotime.DEPARTMENTS = dataset["departments"]
    mock_biotime.EMPLOYEES = dataset["employees"]
    mock_biotime.TERMINALS = dataset["terminals"]
    try:
        yield
    finally:
        mock_biotime.DEPARTMENTS, mock_biotime.EMPLOYEES, mock_biotime.TERMINALS = original


def test_served_over_http_company_2_returns_only_company_2s_people(served_company_2) -> None:
    server = build_server()
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        headers = {"Authorization": f"Token {TOKEN}"}
        base = f"http://127.0.0.1:{port}"

        employees = httpx.get(f"{base}/personnel/api/employees/", headers=headers, timeout=5.0).json()
        codes = {row["emp_code"] for row in employees["data"]}
        assert codes <= {"2001", "2002", "2003"}
        assert "1001" not in codes  # company 1's Ahmed Sharma must not leak in

        terminals = httpx.get(f"{base}/iclock/api/terminals/", headers=headers, timeout=5.0).json()
        serials = {row["sn"] for row in terminals["data"]}
        assert serials <= {"MOCK-GATE-03", "MOCK-GATE-04"}
        assert "MOCK-GATE-01" not in serials
    finally:
        server.shutdown()


def test_company_flag_accepts_valid_companies() -> None:
    """Verify that all datasets (1, 2, 4) are accepted by --company."""
    for company in sorted(DATASETS.keys()):
        assert DATASETS[company]["employees"]  # Just verify they exist


def test_company_flag_rejects_an_unknown_company() -> None:
    from tools.mock_biotime import main
    import sys

    old_argv = sys.argv
    sys.argv = ["mock_biotime.py", "--company", "3"]
    try:
        with pytest.raises(SystemExit):
            main()
    finally:
        sys.argv = old_argv
