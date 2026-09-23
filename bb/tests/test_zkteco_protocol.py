"""Protocol-level coverage for the standalone ZKTeco provider.

There is no physical terminal to test against in this environment, so this
exercises the wire protocol against a small fake device — a real TCP socket
speaking the documented framing (see the module docstring in
app/integrations/providers/zkteco.py for the source) — rather than mocking
the client's own internals. That proves the framing, checksum, chunked
data reassembly and record parsing are internally self-consistent with the
spec; it does not prove any particular vendor's firmware matches it.
"""
from __future__ import annotations

import socket
import struct
import threading
from datetime import datetime

import pytest

from app.integrations.base import EmployeeRecord, SourceConfig
from app.integrations.providers.zkteco import (
    CMD_ACK_OK,
    CMD_ACK_UNAUTH,
    CMD_ATTLOG_RRQ,
    CMD_CONNECT,
    CMD_DATA,
    CMD_DISABLEDEVICE,
    CMD_ENABLEDEVICE,
    CMD_EXIT,
    CMD_FREE_DATA,
    CMD_GET_FREE_SIZES,
    CMD_OPTIONS_RRQ,
    CMD_PREPARE_DATA,
    CMD_REFRESHDATA,
    CMD_USER_WRQ,
    CMD_USERTEMP_RRQ,
    FCT_USER,
    ZKDeviceProvider,
    ZKError,
    _checksum16,
    _CMD_PREPARE_BUFFER,
    _CMD_READ_BUFFER,
    _decode_time,
    _parse_address,
    _USER_STRUCT,
)

MAGIC = b"\x50\x50\x82\x7d"


# ---------------------------------------------------------------------------
# Wire-level helpers, deliberately reimplemented independently of the module
# under test rather than imported from it.
# ---------------------------------------------------------------------------
def _pack(command: int, session_id: int, reply_id: int, data: bytes = b"") -> bytes:
    header = struct.pack("<HHHH", command, 0, session_id, reply_id)
    body = header + data
    return MAGIC + struct.pack("<I", len(body)) + body


def _recv_all(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def _recv_packet(sock: socket.socket) -> tuple[int, int, int, bytes]:
    outer = _recv_all(sock, 8)
    assert outer[:4] == MAGIC
    (length,) = struct.unpack("<I", outer[4:8])
    body = _recv_all(sock, length)
    command, _checksum, session_id, reply_id = struct.unpack("<HHHH", body[:8])
    return command, session_id, reply_id, body[8:]


def _encode_attlog_record(
    user_id: str, when: datetime, verify_type: int = 1, verify_state: int = 0
) -> bytes:
    enc_time = (
        ((when.year % 100) * 12 * 31 + (when.month - 1) * 31 + (when.day - 1)) * 86400
        + (when.hour * 60 + when.minute) * 60
        + when.second
    )
    record = bytearray(40)
    struct.pack_into("<H", record, 0, 1)
    uid_bytes = user_id.encode("ascii")[:9]
    record[2 : 2 + len(uid_bytes)] = uid_bytes
    record[26] = verify_type
    struct.pack_into("<I", record, 27, enc_time)
    record[31] = verify_state
    return bytes(record)


def _encode_user_record(
    uid: int,
    emp_code: str,
    name: str = "",
    privilege: int = 0,
    card_number: int = 0,
    group_id: str = "",
) -> bytes:
    return struct.pack(
        _USER_STRUCT,
        uid,
        privilege,
        b"",
        name.encode("utf-8", "ignore")[:24],
        card_number,
        group_id.encode("ascii", "ignore")[:7],
        emp_code.encode("ascii", "ignore")[:24],
    )


class FakeZKDevice:
    """A minimal ZKTeco terminal, just enough to drive the provider through
    every code path it has: connect, param lookup, free-sizes, and a buffered
    read/write of the attendance log and user table via the real
    CMD_PREPARE_BUFFER/CMD_READ_BUFFER/CMD_FREE_DATA dance (see the module
    docstring in app/integrations/providers/zkteco.py for why this replaced
    an earlier, wrong-shaped fake that sent CMD_ATTLOG_RRQ at the top level).
    """

    def __init__(
        self,
        *,
        params: dict[str, str] | None = None,
        attlog_records: list[bytes] | None = None,
        attlog_chunk_size: int = 40,
        user_records: list[bytes] | None = None,
        require_auth: bool = False,
        attlog_count: int | None = None,
    ) -> None:
        self.params = params or {}
        self.attlog_records = attlog_records or []
        self.attlog_chunk_size = attlog_chunk_size
        self.user_records: list[bytes] = list(user_records or [])
        self.require_auth = require_auth
        self.attlog_count = attlog_count
        self._pending_buffer = b""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def __enter__(self) -> "FakeZKDevice":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        """Accept connections in a loop, one at a time, for as long as the
        listening socket stays open — a real terminal only tolerates one
        *simultaneous* client (see _Connection's docstring in the module
        under test), but happily serves a fresh session right after the
        last one disconnects, which every provider method relies on since
        each opens and closes its own short-lived connection."""
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            try:
                self._serve_one_connection(conn)
            except OSError:
                pass

    def _serve_one_connection(self, conn: socket.socket) -> None:
        session_id = 4242
        with conn:
            while True:
                try:
                    command, _sid, reply_id, data = _recv_packet(conn)
                except (ConnectionError, OSError):
                    return

                if command == CMD_CONNECT:
                    if self.require_auth:
                        conn.sendall(_pack(CMD_ACK_UNAUTH, 0, reply_id))
                        return
                    conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))
                elif command == CMD_EXIT:
                    conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))
                    return
                elif command == CMD_OPTIONS_RRQ:
                    name = data.split(b"\x00", 1)[0].decode("ascii")
                    value = self.params.get(name, "")
                    conn.sendall(
                        _pack(CMD_ACK_OK, session_id, reply_id, f"{name}={value}\x00".encode())
                    )
                elif command == CMD_DISABLEDEVICE:
                    conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))
                elif command == CMD_ENABLEDEVICE:
                    conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))
                elif command == CMD_GET_FREE_SIZES:
                    payload = bytearray(92)
                    if self.attlog_count is not None:
                        struct.pack_into("<I", payload, 32, self.attlog_count)
                    conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id, bytes(payload)))
                elif command == _CMD_PREPARE_BUFFER:
                    self._prepare_buffer(conn, session_id, reply_id, data)
                elif command == _CMD_READ_BUFFER:
                    self._read_buffer_chunk(conn, session_id, reply_id, data)
                elif command == CMD_FREE_DATA:
                    conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))
                elif command == CMD_USER_WRQ:
                    self._write_user(conn, session_id, reply_id, data)
                elif command == CMD_REFRESHDATA:
                    conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))
                else:
                    conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))

    def _prepare_buffer(
        self, conn: socket.socket, session_id: int, reply_id: int, data: bytes
    ) -> None:
        """Handle the outer CMD_PREPARE_BUFFER request that names what a
        following CMD_READ_BUFFER sequence will actually read — see
        _Connection.read_with_buffer's docstring in the module under test."""
        _flag, req_command, fct, _ext = struct.unpack("<bhii", data)
        if req_command == CMD_ATTLOG_RRQ:
            self._pending_buffer = b"".join(self.attlog_records)
        elif req_command == CMD_USERTEMP_RRQ and fct == FCT_USER:
            self._pending_buffer = b"".join(self.user_records)
        else:
            self._pending_buffer = b""
        ack = bytearray(5)
        struct.pack_into("<I", ack, 1, len(self._pending_buffer))
        conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id, bytes(ack)))

    def _read_buffer_chunk(
        self, conn: socket.socket, session_id: int, reply_id: int, data: bytes
    ) -> None:
        start, size = struct.unpack("<ii", data)
        chunk = self._pending_buffer[start : start + size]
        conn.sendall(
            _pack(CMD_PREPARE_DATA, session_id, reply_id, struct.pack("<II", len(chunk), 0x10))
        )
        for offset in range(0, len(chunk), self.attlog_chunk_size):
            piece = chunk[offset : offset + self.attlog_chunk_size]
            # Unsolicited follow-up packets — same reply_id convention doesn't
            # matter here since the client never validates it.
            conn.sendall(_pack(CMD_DATA, session_id, reply_id, piece))
        conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))

    def _write_user(
        self, conn: socket.socket, session_id: int, reply_id: int, data: bytes
    ) -> None:
        (uid,) = struct.unpack_from("<H", data, 0)
        for i, existing in enumerate(self.user_records):
            (existing_uid,) = struct.unpack_from("<H", existing, 0)
            if existing_uid == uid:
                self.user_records[i] = data
                break
        else:
            self.user_records.append(data)
        conn.sendall(_pack(CMD_ACK_OK, session_id, reply_id))


def _provider(device: FakeZKDevice, password: str = "") -> ZKDeviceProvider:
    return ZKDeviceProvider(
        SourceConfig(base_url=f"zk://{device.host}:{device.port}", password=password, timezone="UTC")
    )


# ---------------------------------------------------------------------------
# Checksum / timestamp — pure functions, no socket needed.
# ---------------------------------------------------------------------------
def test_checksum_is_16_bit_and_deterministic():
    body = struct.pack("<HHHH", 1000, 0, 0, 0)
    checksum = _checksum16(body)
    assert 0 <= checksum <= 0xFFFF
    assert _checksum16(body) == checksum  # pure function


def test_checksum_changes_with_payload():
    a = _checksum16(struct.pack("<HHHH", 1000, 0, 0, 0))
    b = _checksum16(struct.pack("<HHHH", 1001, 0, 0, 0))
    assert a != b


def _reference_checksum16(payload: bytes) -> int:
    """Reproduced independently from pyzk's own checksum routine — not just
    self-consistency with the module under test. This project shipped a
    plausible-looking but wrong "sum, fold once, XOR 0xFFFF" version of this
    once (see the module docstring); this reference is deliberately a
    line-for-line port of the real thing rather than a re-derivation, so a
    future refactor can't reintroduce the same mistake and still pass."""
    length = len(payload)
    checksum = 0
    p = payload
    while length > 1:
        checksum += struct.unpack("H", struct.pack("BB", p[0], p[1]))[0]
        p = p[2:]
        if checksum > 0xFFFF:
            checksum -= 0xFFFF
        length -= 2
    if length:
        checksum = checksum + p[-1]
    while checksum > 0xFFFF:
        checksum -= 0xFFFF
    checksum = ~checksum
    while checksum < 0:
        checksum += 0xFFFF
    return checksum


@pytest.mark.parametrize(
    "payload",
    [
        struct.pack("<HHHH", 1000, 0, 0, 0),
        struct.pack("<HHHH", 13, 0, 4242, 7),
        b"\x01\x0d\x00\x00\x05\x00\x00\x00\x00\x00\x00",  # a CMD_PREPARE_BUFFER-shaped payload
        bytes(range(72)),  # odd trailing byte, and a full user record's width
    ],
)
def test_checksum_matches_independent_reference_implementation(payload: bytes):
    assert _checksum16(payload) == _reference_checksum16(payload)


def test_checksum_matches_pinned_known_values():
    """Pins concrete outputs so a regression to the old, wrong finalization
    fails immediately here rather than only against real hardware."""
    assert _checksum16(struct.pack("<HHHH", 1000, 0, 0, 0)) == 64534
    assert _checksum16(struct.pack("<HHHH", 13, 0, 4242, 7)) == 61272
    assert _checksum16(b"\x01\x0d\x00\x00\x05\x00\x00\x00\x00\x00\x00") == 62200
    assert _checksum16(bytes(range(72))) == 60173


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 9, 23, 14, 5, 30),
        datetime(2024, 2, 29, 0, 0, 0),  # leap day
        datetime(2026, 1, 1, 23, 59, 59),
        datetime(2099, 12, 31, 12, 0, 0),  # top of the base-2000 range
    ],
)
def test_decode_time_matches_device_encoding(when: datetime):
    enc_t = (
        ((when.year % 100) * 12 * 31 + (when.month - 1) * 31 + (when.day - 1)) * 86400
        + (when.hour * 60 + when.minute) * 60
        + when.second
    )
    assert _decode_time(enc_t) == when


def test_parse_address_variants():
    assert _parse_address("zk://192.168.1.50:4370") == ("192.168.1.50", 4370)
    assert _parse_address("zk://192.168.1.50") == ("192.168.1.50", 4370)
    assert _parse_address("192.168.1.50:9999") == ("192.168.1.50", 9999)
    assert _parse_address("192.168.1.50") == ("192.168.1.50", 4370)
    with pytest.raises(ZKError):
        _parse_address("")


def test_non_numeric_comm_key_rejected():
    with pytest.raises(ZKError):
        ZKDeviceProvider(SourceConfig(base_url="zk://127.0.0.1:4370", password="not-a-number"))


# ---------------------------------------------------------------------------
# Against the fake device.
# ---------------------------------------------------------------------------
def test_connection_reports_serial_and_count():
    with FakeZKDevice(params={"~SerialNumber": "ABC123"}, attlog_count=7) as device:
        result = _provider(device).test_connection()
    assert result.ok
    assert "ABC123" in result.message
    assert "7" in result.message


def test_connection_requiring_comm_key_gives_a_clear_error():
    with FakeZKDevice(require_auth=True) as device:
        result = _provider(device).test_connection()
    assert not result.ok
    assert "comm key" in result.message.lower() or "communication password" in result.message.lower()


def test_serial_not_configured_falls_back_to_host():
    with FakeZKDevice() as device:  # no ~SerialNumber configured
        result = _provider(device).test_connection()
    assert result.ok
    assert device.host in result.message


def test_fetch_punches_reassembles_multi_chunk_log_and_normalises_events():
    records = [
        _encode_attlog_record("111", datetime(2026, 9, 1, 8, 0, 0), verify_type=1, verify_state=0),
        _encode_attlog_record("111", datetime(2026, 9, 1, 17, 0, 0), verify_type=1, verify_state=1),
        _encode_attlog_record("222", datetime(2026, 9, 1, 9, 30, 0), verify_type=2, verify_state=0),
    ]
    with FakeZKDevice(
        params={"~SerialNumber": "TERM-1"}, attlog_records=records, attlog_chunk_size=40
    ) as device:
        events = list(_provider(device).fetch_punches())

    assert len(events) == 3
    first = events[0]
    assert first.emp_code == "111"
    assert first.punch_time_local == datetime(2026, 9, 1, 8, 0, 0)
    assert first.direction is True
    assert first.terminal_sn == "TERM-1"
    assert first.verify_type == "fingerprint"
    assert events[1].direction is False
    assert events[2].emp_code == "222"
    assert events[2].verify_type == "card"
    # Every record must produce a distinct, stable identity so a re-fetch of
    # the same (never-cleared) log dedups instead of double-ingesting.
    assert len({e.external_id for e in events}) == 3


def test_fetch_punches_reassembles_when_split_mid_record():
    """The device is free to chunk however it likes — chunk boundaries need
    not land on record boundaries."""
    records = [
        _encode_attlog_record("111", datetime(2026, 9, 1, 8, 0, 0)),
        _encode_attlog_record("222", datetime(2026, 9, 1, 9, 0, 0)),
    ]
    with FakeZKDevice(attlog_records=records, attlog_chunk_size=25) as device:
        events = list(_provider(device).fetch_punches())
    assert [e.emp_code for e in events] == ["111", "222"]


def test_fetch_punches_filters_by_since_and_until():
    records = [
        _encode_attlog_record("1", datetime(2026, 1, 1, 0, 0, 0)),
        _encode_attlog_record("2", datetime(2026, 6, 1, 0, 0, 0)),
        _encode_attlog_record("3", datetime(2026, 12, 1, 0, 0, 0)),
    ]
    with FakeZKDevice(attlog_records=records) as device:
        events = list(
            _provider(device).fetch_punches(
                since=datetime(2026, 3, 1), until=datetime(2026, 9, 1)
            )
        )
    assert [e.emp_code for e in events] == ["2"]


def test_fetch_punches_empty_log():
    with FakeZKDevice(attlog_records=[]) as device:
        events = list(_provider(device).fetch_punches())
    assert events == []


def test_fetch_terminals_returns_one_record_for_the_device_itself():
    with FakeZKDevice(params={"~SerialNumber": "TERM-9", "~DeviceName": "Front Door"}) as device:
        terminals = list(_provider(device).fetch_terminals())
    assert len(terminals) == 1
    assert terminals[0].serial_number == "TERM-9"
    assert terminals[0].alias == "Front Door"
    assert terminals[0].ip_address == device.host


def test_connect_refused_gives_a_actionable_message():
    # Nothing is listening on this port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()  # freed immediately, so the connect below is refused

    provider = ZKDeviceProvider(SourceConfig(base_url=f"zk://{host}:{port}"))
    result = provider.test_connection()
    assert not result.ok
    assert str(port) in result.message or host in result.message


# ---------------------------------------------------------------------------
# Employee provisioning (fetch_employees / create_employee) — the buffered
# user-table read shares read_with_buffer with fetch_punches above, but gets
# its own coverage since the record layout and the write path are new.
# ---------------------------------------------------------------------------
def test_fetch_employees_parses_the_user_table():
    records = [
        _encode_user_record(1, "111", "Alice Smith", card_number=555),
        _encode_user_record(2, "222", "Bob Jones"),
    ]
    with FakeZKDevice(user_records=records) as device:
        employees = list(_provider(device).fetch_employees())

    assert len(employees) == 2
    alice = next(e for e in employees if e.emp_code == "111")
    assert alice.first_name == "Alice"
    assert alice.last_name == "Smith"
    assert alice.external_id == "1"
    assert alice.raw["card_number"] == 555
    assert alice.is_active is True


def test_fetch_employees_empty_table():
    with FakeZKDevice(user_records=[]) as device:
        employees = list(_provider(device).fetch_employees())
    assert employees == []


def test_create_employee_assigns_next_uid_and_persists_on_device():
    existing = [_encode_user_record(1, "111", "Alice Smith"), _encode_user_record(5, "222", "Bob Jones")]
    with FakeZKDevice(user_records=existing) as device:
        provider = _provider(device)
        created = provider.create_employee(EmployeeRecord(external_id=None, emp_code="NEW1", first_name="New", last_name="Hire"))
        assert created.emp_code == "NEW1"
        assert created.external_id == "6"  # max(existing uids) + 1

        # And it's really on the device now, readable back like any other user.
        refetched = list(provider.fetch_employees())
    codes = {e.emp_code: e for e in refetched}
    assert "NEW1" in codes
    assert codes["NEW1"].raw["uid"] == 6
    assert codes["NEW1"].first_name == "New"


def test_create_employee_first_user_gets_uid_one():
    with FakeZKDevice(user_records=[]) as device:
        created = _provider(device).create_employee(EmployeeRecord(external_id=None, emp_code="A1"))
    assert created.external_id == "1"


def test_create_employee_is_idempotent_for_an_already_provisioned_code():
    existing = [_encode_user_record(3, "DUPE", "Already Here")]
    with FakeZKDevice(user_records=existing) as device:
        provider = _provider(device)
        result = provider.create_employee(EmployeeRecord(external_id=None, emp_code="DUPE", first_name="New", last_name="Name"))
        # Returns the existing record rather than creating a second one.
        assert result.external_id == "3"
        assert result.first_name == "Already"
        all_employees = list(provider.fetch_employees())
    assert len(all_employees) == 1


def test_create_employee_rejects_codes_longer_than_the_attlog_can_carry():
    with FakeZKDevice(user_records=[]) as device:
        provider = _provider(device)
        with pytest.raises(ZKError, match="characters"):
            provider.create_employee(EmployeeRecord(external_id=None, emp_code="TOOLONGCODE12"))


def test_create_employee_rejects_blank_emp_code():
    with FakeZKDevice(user_records=[]) as device:
        provider = _provider(device)
        with pytest.raises(ZKError):
            provider.create_employee(EmployeeRecord(external_id=None, emp_code="   "))
