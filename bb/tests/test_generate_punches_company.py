"""tools/generate_punches.py must be able to generate for company 2's roster.

Pairs with tests/test_mock_biotime_company.py: a mock server started with
--company 2 is only useful for a demo or a manual sync if the punches fed to
it actually carry company 2's emp_codes and terminal serials, not company 1's
leftover defaults.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from tools.generate_punches import COMPANY_EMP_CODES, COMPANY_TERMINALS, build


def test_company_1_default_terminals_are_unchanged() -> None:
    """No --terminals argument existed before this feature — passing None
    must still fall back to exactly the roster this file always used."""
    rows, _ = build(
        emp_codes=COMPANY_EMP_CODES[1], days=1, check_in="09:00", check_out="17:00",
        tz=ZoneInfo("Asia/Dubai"), jitter_minutes=0, skip_weekends=False, seed=1,
        terminals=None,
    )
    assert {r["terminal_sn"] for r in rows} <= {"MOCK-GATE-01", "MOCK-GATE-02"}


def test_company_2_codes_and_terminals_never_collide_with_company_1() -> None:
    codes_1, codes_2 = set(COMPANY_EMP_CODES[1]), set(COMPANY_EMP_CODES[2])
    terms_1, terms_2 = set(COMPANY_TERMINALS[1]), set(COMPANY_TERMINALS[2])
    assert codes_1.isdisjoint(codes_2)
    assert terms_1.isdisjoint(terms_2)


def test_build_with_company_2_terminals_only_uses_company_2_terminals() -> None:
    rows, _ = build(
        emp_codes=COMPANY_EMP_CODES[2], days=3, check_in="09:00", check_out="17:00",
        tz=ZoneInfo("Asia/Dubai"), jitter_minutes=0, skip_weekends=False, seed=1,
        terminals=COMPANY_TERMINALS[2],
    )
    assert rows, "expected at least one punch across 3 days"
    serials = {r["terminal_sn"] for r in rows}
    assert serials <= set(COMPANY_TERMINALS[2])
    assert serials.isdisjoint(COMPANY_TERMINALS[1])


def test_cli_defaults_out_filename_by_company(tmp_path, monkeypatch) -> None:
    """--out isn't given, so company 2's file must not collide with company
    1's punches.json — two mocks generating side by side would otherwise
    silently overwrite each other's punch data."""
    import sys

    from tools.generate_punches import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["generate_punches.py", "--company", "2", "--seed", "1"])
    assert main() == 0
    assert (tmp_path / "punches_company2.json").exists()
    assert not (tmp_path / "punches.json").exists()
