"""The mock BioTime's live punches: the /mock/punch trigger and --simulate.

What matters is what a *sync* depends on: ids that keep climbing (BioBridge
skips an id it has already stored), punches stamped "now" (a fetch window ends
at now), and a person's punches alternating in / out so Odoo accepts them.
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

import tools.mock_biotime as mock
from tests.test_mock_biotime_company import _free_port
from tools.mock_biotime import TOKEN, build_server


@pytest.fixture()
def live(tmp_path, monkeypatch):
    path = tmp_path / "punches.json"
    path.write_text("[]")
    monkeypatch.setattr(mock, "PUNCH_FILE", str(path))
    monkeypatch.setattr(mock, "SERVER_TZ", "Asia/Kolkata")
    monkeypatch.setattr(mock, "EMPLOYEES", [mock._emp(1, "5", "Beevi", "", None),
                                            mock._emp(2, "6001", "Marc", "", None)])
    monkeypatch.setattr(mock, "TERMINALS", [mock._term(1, "MOCK-GATE-05", "East", "10.0.0.1")])
    server = build_server("127.0.0.1", _free_port())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, path
    server.shutdown()


def test_a_manual_punch_toggles_in_then_out_and_ids_keep_climbing(live):
    base, path = live
    first = httpx.post(f"{base}/mock/punch", json={"emp_code": "5"}).json()
    second = httpx.post(f"{base}/mock/punch", json={"emp_code": "5"}).json()
    third = httpx.post(f"{base}/mock/punch", json={"emp_code": "5"}).json()
    assert [first["punch_state"], second["punch_state"], third["punch_state"]] == ["0", "1", "0"]
    assert first["id"] < second["id"] < third["id"]
    assert first["terminal_sn"] == "MOCK-GATE-05"
    # Never a tiny id a previous run could already have used.
    assert first["id"] > 1_000_000_000
    assert len(json.loads(path.read_text())) == 3


def test_a_punch_can_name_its_state_and_is_served_to_a_sync(live):
    base, _ = live
    httpx.post(f"{base}/mock/punch", json={"emp_code": "6001", "state": "in"})
    out = httpx.post(f"{base}/mock/punch", json={"emp_code": "6001", "state": "out"}).json()
    assert out["punch_state"] == "1"
    served = httpx.get(f"{base}/iclock/api/transactions/", headers={"Authorization": f"Token {TOKEN}"}).json()
    assert [r["punch_state"] for r in served["data"]] == ["0", "1"] or served["count"] == 2


def test_bad_punches_are_refused(live):
    base, _ = live
    assert httpx.post(f"{base}/mock/punch", json={}).status_code == 400
    assert httpx.post(f"{base}/mock/punch", json={"emp_code": "5", "state": "sideways"}).status_code == 400


def test_the_state_page_says_who_is_in(live):
    base, _ = live
    httpx.post(f"{base}/mock/punch", json={"emp_code": "5"})
    state = httpx.get(f"{base}/mock/state").json()
    assert state["people"]["5"]["now"] == "checked in"
    assert state["people"]["6001"]["now"] == "no punches"
    assert state["punches"] == 1


def test_the_simulator_produces_alternating_punches_stamped_now(live):
    base, path = live
    stop = threading.Event()
    worker = threading.Thread(target=mock.simulate, args=(0.05, stop, 2), daemon=True)
    worker.start()
    time.sleep(0.6)
    stop.set()
    rows = json.loads(path.read_text())
    assert rows, "the simulator wrote nothing"
    ids = [r["id"] for r in rows]
    assert ids == sorted(set(ids)), "ids climb and never repeat"
    for code in {r["emp_code"] for r in rows}:
        states = [r["punch_state"] for r in sorted((r for r in rows if r["emp_code"] == code),
                                                   key=lambda r: r["id"])]
        assert states[0] == "0" and all(a != b for a, b in zip(states, states[1:])), states


def test_regenerating_a_file_continues_its_ids(tmp_path):
    from tools.generate_punches import build
    from zoneinfo import ZoneInfo
    rows, _ = build(["5"], 2, "00:01", "00:02", ZoneInfo("Asia/Kolkata"), 0, False, 1, first_id=500)
    assert min(r["id"] for r in rows) == 500
