"""hr.attendance in_mode / out_mode: device punches are "technical"; a
check-out BioBridge made up (an auto-closed shift) is "manual"."""
from app.integrations.odoo import OdooClient, OdooCredentials


def client_with(selection):
    c = OdooClient(OdooCredentials(url="https://a.odoo.com", db="a", username="b", api_key="k", uid=1))
    c._field_cache["hr.attendance"] = {"in_mode", "out_mode"} if selection is not None else set()
    c.execute = lambda model, method, args=None, kwargs=None: (
        {"in_mode": {"selection": selection}} if method == "fields_get" else None)
    return c


FULL = [["kiosk", "Kiosk"], ["systray", "Systray"], ["manual", "Manual"], ["technical", "Technical"]]


def test_punch_times_are_technical():
    c = client_with(FULL)
    assert c._mode_vals(check_in=True) == {"in_mode": "technical"}
    assert c._mode_vals(check_in=True, check_out=True) == {"in_mode": "technical", "out_mode": "technical"}
    assert c._mode_vals(check_out=True) == {"out_mode": "technical"}


def test_auto_closed_checkout_is_manual():
    c = client_with(FULL)
    assert c._mode_vals(check_out=True, auto_closed=True) == {"out_mode": "manual"}
    assert c._mode_vals(check_in=True, check_out=True, auto_closed=True) == {
        "in_mode": "technical", "out_mode": "manual"}


def test_reopen_clears_out_mode():
    assert client_with(FULL)._mode_vals(reopen=True) == {"out_mode": False}


def test_old_odoo_without_modes_or_technical_gets_nothing():
    assert client_with(None)._mode_vals(check_in=True, check_out=True) == {}
    no_tech = [["kiosk", "Kiosk"], ["systray", "Systray"], ["manual", "Manual"]]
    assert client_with(no_tech)._mode_vals(check_in=True) == {}


def test_state_based_two_check_ins_close_the_first_as_auto_closed():
    from datetime import datetime
    from app.services.pairing import Direction, PairingConfig, PairingMode, Punch, pair_punches

    def p(i, hour, d):
        return Punch(str(i), "1", datetime(2026, 10, 9, hour), d)

    inward, outward = Direction.inward, Direction.outward
    result = pair_punches(
        [p(1, 8, inward), p(2, 12, inward), p(3, 17, outward)],
        PairingConfig(mode=PairingMode.state_based),
    )
    first, second = result.intervals
    assert first.check_out == datetime(2026, 10, 9, 12) and first.auto_closed
    assert second.check_out == datetime(2026, 10, 9, 17) and not second.auto_closed
