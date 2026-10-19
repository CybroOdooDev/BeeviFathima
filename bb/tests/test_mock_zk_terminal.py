"""The simulated ZK terminal, exercised through BioBridge's own ZK client.

If the real provider can connect to it, read its serial, users and log, and
provision a user on it, the simulator is faithful enough to stand in for a unit.
"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from app.integrations.base import EmployeeRecord, SourceConfig
from app.integrations.providers.zkteco import ZKDeviceProvider
from tools import mock_zk_terminal as sim


@pytest.fixture(params=["modern", "legacy"])
def terminal(request, tmp_path):
    device = sim.Device("MOCK-ACME-C1-ZK-01", "UTC", request.param, comm_key=0, log_path=tmp_path / "zk.json")
    device.enroll("101", "Jane Doe")
    device.enroll("102", "Omar Haddad")
    server = sim.build_server(device, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield device, server.server_address[1]
    server.shutdown()
    server.server_close()


def provider(port, key=""):
    return ZKDeviceProvider(SourceConfig(base_url=f"zk://127.0.0.1:{port}", password=key, timezone="UTC"))


def test_connection_reports_the_serial_and_log_size(terminal):
    device, port = terminal
    device.punch("101", at=datetime(2026, 10, 15, 8, 0, 0))
    info = provider(port).test_connection()
    assert info.ok, info.message
    assert "MOCK-ACME-C1-ZK-01" in info.message and "1 attendance record" in info.message


def test_terminal_record_has_the_serial_and_device_name(terminal):
    _, port = terminal
    (record,) = list(provider(port).fetch_terminals())
    assert record.serial_number == "MOCK-ACME-C1-ZK-01"
    assert record.alias == sim.DEVICE_NAME


def test_punches_round_trip_with_user_ids_times_and_direction(terminal):
    device, port = terminal
    device.punch("101", sim.STATE_IN, datetime(2026, 10, 15, 8, 5, 9))
    device.punch("102", sim.STATE_IN, datetime(2026, 10, 15, 8, 30, 0))
    device.punch("101", sim.STATE_OUT, datetime(2026, 10, 15, 17, 1, 2))

    events = list(provider(port).fetch_punches())
    assert [(e.emp_code, e.punch_time_local, e.direction) for e in events] == [
        ("101", datetime(2026, 10, 15, 8, 5, 9), True),
        ("102", datetime(2026, 10, 15, 8, 30, 0), True),
        ("101", datetime(2026, 10, 15, 17, 1, 2), False),
    ]
    assert {e.terminal_sn for e in events} == {"MOCK-ACME-C1-ZK-01"}
    assert len({e.external_id for e in events}) == 3, "each punch needs its own id"


def test_an_empty_log_yields_nothing(terminal):
    _, port = terminal
    assert list(provider(port).fetch_punches()) == []


def test_users_are_readable_and_a_new_one_can_be_provisioned(terminal):
    device, port = terminal
    codes = {e.emp_code for e in provider(port).fetch_employees()}
    assert codes == {"101", "102"}

    created = provider(port).create_employee(EmployeeRecord(external_id="", emp_code="103", first_name="Lina", last_name="K"))
    assert created.emp_code == "103"
    assert {u["user_id"] for u in device.users} == {"101", "102", "103"}
    assert {e.emp_code for e in provider(port).fetch_employees()} == {"101", "102", "103"}


def test_a_punch_for_an_unenrolled_user_is_refused(terminal):
    device, _ = terminal
    with pytest.raises(ValueError):
        device.punch("999")


def test_toggle_alternates_in_and_out(terminal):
    device, _ = terminal
    assert device.punch("101")["state"] == sim.STATE_IN
    assert device.punch("101")["state"] == sim.STATE_OUT
    assert device.punch("101")["state"] == sim.STATE_IN


def test_a_wrong_comm_key_is_rejected_and_the_right_one_accepted(tmp_path):
    device = sim.Device("MOCK-ACME-C1-ZK-01", "UTC", "modern", comm_key=1234, log_path=None)
    server = sim.build_server(device, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        bad = provider(port, key="1")
        assert not bad.test_connection().ok and "comm key" in bad.test_connection().message.lower()
        assert provider(port, key="1234").test_connection().ok
    finally:
        server.shutdown()
        server.server_close()


def test_an_offline_device_looks_down_then_recovers(terminal, monkeypatch):
    device, port = terminal
    import app.integrations.providers.zkteco as zk
    monkeypatch.setattr(zk, "SOCKET_TIMEOUT", 2)
    device.go_offline()
    down = provider(port).test_connection()
    assert not down.ok
    device.offline = False
    assert provider(port).test_connection().ok


def test_the_log_survives_a_restart(tmp_path):
    path = tmp_path / "zk.json"
    first = sim.Device("SN", "UTC", "modern", 0, path)
    first.enroll("101", "Jane Doe")
    first.punch("101", sim.STATE_IN, datetime(2026, 10, 15, 8, 0, 0))
    second = sim.Device("SN", "UTC", "modern", 0, path)
    assert len(second.log) == 1 and second.users[0]["user_id"] == "101"


def test_legacy_firmware_refuses_a_non_numeric_id(tmp_path):
    legacy = sim.Device("SN", "UTC", "legacy", 0, None)
    assert legacy.enroll("EMP-7", "Someone") is None
    assert sim.Device("SN", "UTC", "modern", 0, None).enroll("EMP-7", "Someone") is not None
