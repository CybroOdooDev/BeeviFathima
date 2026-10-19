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
    CMD_AUTH,
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
    _make_commkey,
    _CMD_PREPARE_BUFFER,
    _CMD_READ_BUFFER,
    _Connection,
    _decode_time,
    _parse_address,
    _split_table,
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


def _recv_packet(sock: socket.socket) -> tuple[int, int, int, bytes, bool]:
    outer = _recv_all(sock, 8)
    assert outer[:4] == MAGIC
    (length,) = struct.unpack("<I", outer[4:8])
    body = _recv_all(sock, length)
    command, checksum, session_id, reply_id = struct.unpack("<HHHH", body[:8])
    return command, session_id, reply_id, body[8:], checksum == _expected_checksum(
        command, session_id, reply_id, body[8:]
    )


def _expected_checksum(command: int, session_id: int, reply_id: int, data: bytes) -> int:
    """What a real terminal accepts: the checksum over the header carrying
    the reply id *before* this one (mod 65535), not the one in the packet.
    Taken from pyzk's __create_header, the client that connected to a real
    terminal which silently ignored a checksum computed over the sent id —
    see "The reply-id checksum rule" in the module docstring."""
    previous = (reply_id - 1) % 65535
    return _reference_checksum16(struct.pack("<HHHH", command, 0, session_id, previous) + data)


def _enc_time(when: datetime) -> int:
    return (
        ((when.year % 100) * 12 * 31 + (when.month - 1) * 31 + (when.day - 1)) * 86400
        + (when.hour * 60 + when.minute) * 60
        + when.second
    )


# Attendance record encoders, one per firmware layout. Written as explicit
# struct formats from pyzk 0.9's get_attendance, independently of the module
# under test's own constants.
def _encode_attlog_record(
    user_id: str, when: datetime, verify_type: int = 1, verify_state: int = 0, uid: int = 1
) -> bytes:
    """40-byte layout: uid(H) user_id(24s) verify(B) time(I) state(B) 8 reserved."""
    return struct.pack(
        "<H24sBIB8s", uid, user_id.encode("ascii"), verify_type, _enc_time(when),
        verify_state, b"",
    )


def _encode_attlog_record_16(
    user_id: int, when: datetime, verify_type: int = 1, verify_state: int = 0
) -> bytes:
    """16-byte layout: user_id(I) time(I) verify(B) state(B) 2 reserved workcode(I)."""
    return struct.pack("<IIBB2sI", user_id, _enc_time(when), verify_type, verify_state, b"", 0)


def _encode_attlog_record_8(
    uid: int, when: datetime, verify_type: int = 1, verify_state: int = 0
) -> bytes:
    """8-byte layout: uid(H) verify(B) time(I) state(B) — a uid, not a user id."""
    return struct.pack("<HBIB", uid, verify_type, _enc_time(when), verify_state)


def _encode_user_record_28(uid: int, user_id: int, name: str = "") -> bytes:
    """28-byte layout: uid(H) priv(B) pw(5s) name(8s) card(I) pad group(B) tz(H) user_id(I)."""
    return struct.pack("<HB5s8sIxBHI", uid, 0, b"", name.encode()[:8], 0, 1, 0, user_id)


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
        comm_key: int = 0,
        attlog_count: int | None = None,
    ) -> None:
        self.params = params or {}
        self.attlog_records = attlog_records or []
        self.attlog_chunk_size = attlog_chunk_size
        self.user_records: list[bytes] = list(user_records or [])
        #: Answer CMD_CONNECT with CMD_ACK_UNAUTH and demand CMD_AUTH, the
        #: way the real terminal this was piloted against did — with its
        #: comm key at 0. ``comm_key`` is what CMD_AUTH must carry.
        self.require_auth = require_auth
        self.comm_key = comm_key
        self.attlog_count = attlog_count
        self._pending_buffer = b""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        #: Packets rejected for a bad checksum. A real terminal drops these
        #: silently and the client times out; this fake closes the
        #: connection instead, so a checksum regression fails the suite in
        #: milliseconds rather than hanging each test for SOCKET_TIMEOUT.
        self.bad_checksums = 0
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
        authed = not self.require_auth
        with conn:
            while True:
                try:
                    command, _sid, reply_id, data, checksum_ok = _recv_packet(conn)
                except (ConnectionError, OSError):
                    return
                if not checksum_ok:
                    self.bad_checksums += 1
                    return
                if not authed and command not in (CMD_CONNECT, CMD_AUTH, CMD_EXIT):
                    conn.sendall(_pack(CMD_ACK_UNAUTH, session_id, reply_id))
                    continue

                if command == CMD_CONNECT:
                    # The session id is assigned here either way — CMD_AUTH
                    # needs it — just as a real terminal does.
                    reply = CMD_ACK_UNAUTH if self.require_auth else CMD_ACK_OK
                    conn.sendall(_pack(reply, session_id, reply_id))
                elif command == CMD_AUTH:
                    authed = data == _reference_commkey(self.comm_key, session_id)
                    conn.sendall(
                        _pack(CMD_ACK_OK if authed else CMD_ACK_UNAUTH, session_id, reply_id)
                    )
                    if not authed:
                        return
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
                    # pyzk's read_sizes layout: 20 int32s, users at field 4,
                    # attendance records at field 8.
                    payload = bytearray(92)
                    records = (
                        self.attlog_count if self.attlog_count is not None
                        else len(self.attlog_records)
                    )
                    struct.pack_into("<i", payload, 16, len(self.user_records))
                    struct.pack_into("<i", payload, 32, records)
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
            table = b"".join(self.attlog_records)
        elif req_command == CMD_USERTEMP_RRQ and fct == FCT_USER:
            table = b"".join(self.user_records)
        else:
            table = None
        # Real tables start with a 4-byte total size (see pyzk's
        # get_attendance/get_users). The fake used to send the records bare,
        # which is how a client that never stripped the prefix passed here
        # and then read every record 4 bytes out of line on real hardware.
        self._pending_buffer = b"" if table is None else struct.pack("<I", len(table)) + table
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


def _reference_commkey(key: int, session_id: int, ticks: int = 50) -> bytes:
    """pyzk 0.9's make_commkey, ported line for line and kept separate from
    the module under test, so the fake device checks CMD_AUTH against the
    reference rather than against the client's own copy."""
    k = 0
    for i in range(32):
        if key & (1 << i):
            k = (k << 1 | 1)
        else:
            k = k << 1
    k += session_id
    k = struct.unpack("BBBB", struct.pack("<I", k))
    k = struct.pack("BBBB", k[0] ^ ord("Z"), k[1] ^ ord("K"), k[2] ^ ord("S"), k[3] ^ ord("O"))
    k = struct.unpack("<HH", k)
    k = struct.pack("<HH", k[1], k[0])
    b = 0xFF & ticks
    k = struct.unpack("BBBB", k)
    return struct.pack("BBBB", k[0] ^ b, k[1] ^ b, b, k[3] ^ b)


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


class _CapturingSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


# Captured from pyzk 0.9's own ZK._ZK__create_header/_ZK__create_tcp_top —
# the client that connected to a real terminal which ignored this module's
# packets. Pinned as bytes so the test needs no pyzk install, and so the
# comparison is against what real hardware accepted, not against this
# module's own idea of the protocol.
_PYZK_CONNECT = bytes.fromhex("5050827d08000000e80317fc00000000")
_PYZK_SERIAL_QUERY = bytes.fromhex(
    "5050827d160000000b005ea6921001007e53657269616c4e756d62657200"
)


def test_first_two_packets_match_pyzk_byte_for_byte():
    """Regression for a real terminal (comm key unset, correct port, ADMS
    off) that accepted the TCP connection and never answered CMD_CONNECT,
    while pyzk connected instantly. The packets differed by one checksum
    byte: pyzk checksums the header carrying the *previous* reply id and
    sends the next one. See _Connection._send."""
    conn = _Connection("device.test", 4370, comm_key=0)
    sock = _CapturingSocket()
    conn.sock = sock  # type: ignore[assignment]

    conn._send(CMD_CONNECT, b"")
    assert sock.sent[0] == _PYZK_CONNECT

    # State after the device answers CMD_CONNECT with session 4242, echoing
    # reply id 0 — what _exchange() records, and what pyzk records.
    conn.session_id = 4242
    conn._reply_id = 0
    conn._send(CMD_OPTIONS_RRQ, b"~SerialNumber\x00")
    assert sock.sent[1] == _PYZK_SERIAL_QUERY


def test_fake_device_rejects_a_checksum_over_the_sent_reply_id():
    """The fake used to accept any checksum, which is how the rule above
    went unnoticed. Prove it now rejects the old, wrong form — so every other
    test in this file is also a checksum test."""
    wrong_header = struct.pack("<HHHH", CMD_CONNECT, 0, 0, 0)
    wrong = struct.pack("<HHHH", CMD_CONNECT, _reference_checksum16(wrong_header), 0, 0)
    with FakeZKDevice() as device:
        with socket.create_connection((device.host, device.port), timeout=2) as sock:
            sock.sendall(MAGIC + struct.pack("<I", len(wrong)) + wrong)
            assert sock.recv(16) == b""  # closed without a reply
    assert device.bad_checksums == 1


def test_a_whole_session_passes_checksum_validation():
    with FakeZKDevice(params={"~SerialNumber": "ABC123"}, attlog_count=3) as device:
        result = _provider(device).test_connection()
    assert result.ok, result.message
    assert device.bad_checksums == 0


def test_device_demanding_auth_with_comm_key_zero_connects():
    """Regression for the real terminal this was piloted against: comm key
    shown as 0 on the device, yet it answered CMD_CONNECT with
    CMD_ACK_UNAUTH. The module used to refuse that outright and tell the
    user to set their comm key to 0 — which it already was. pyzk answers
    with CMD_AUTH carrying the scrambled key 0, and so must this."""
    with FakeZKDevice(require_auth=True, comm_key=0, params={"~SerialNumber": "OIN7"}) as device:
        result = _provider(device).test_connection()
    assert result.ok, result.message
    assert "OIN7" in result.message


def test_device_demanding_auth_accepts_its_configured_comm_key():
    with FakeZKDevice(require_auth=True, comm_key=123456, attlog_records=[
        _encode_attlog_record("7", datetime(2026, 9, 1, 9, 0)),
    ]) as device:
        provider = _provider(device, password="123456")
        assert provider.test_connection().ok
        # Every connection authenticates, not just the probe.
        assert [e.emp_code for e in provider.fetch_punches()] == ["7"]


def test_wrong_comm_key_gives_a_clear_rejection():
    with FakeZKDevice(require_auth=True, comm_key=123456) as device:
        result = _provider(device, password="999").test_connection()
    assert not result.ok
    assert "rejected the comm key" in result.message


def test_make_commkey_matches_pyzk():
    # Captured from pyzk 0.9's make_commkey(key, 4242).
    assert _make_commkey(0, 4242) == bytes.fromhex("617d3269")
    assert _make_commkey(123456, 4242) == bytes.fromhex("267f32e9")
    for key in (0, 1, 7, 123456, 99999999):
        for session in (0, 1, 4242, 65534):
            assert _make_commkey(key, session) == _reference_commkey(key, session)


def test_auth_packet_matches_pyzk_byte_for_byte():
    # pyzk's CMD_AUTH after a CONNECT reply of session 4242 / reply id 0.
    conn = _Connection("device.test", 4370, comm_key=0)
    sock = _CapturingSocket()
    conn.sock = sock  # type: ignore[assignment]
    conn.session_id, conn._reply_id = 4242, 0
    conn._send(CMD_AUTH, _make_commkey(0, 4242))
    assert sock.sent[0] == bytes.fromhex("5050827d0c0000004e048b0492100100617d3269")


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


# ---------------------------------------------------------------------------
# Firmware layouts. Record size isn't in the data; like pyzk, it's the table's
# 4-byte total size divided by the record count from CMD_GET_FREE_SIZES.
# ---------------------------------------------------------------------------
def test_fetch_punches_reads_the_16_byte_layout():
    records = [
        _encode_attlog_record_16(1001, datetime(2026, 9, 1, 8, 0), verify_type=1, verify_state=0),
        _encode_attlog_record_16(1002, datetime(2026, 9, 1, 17, 30), verify_type=15, verify_state=1),
    ]
    with FakeZKDevice(attlog_records=records) as device:
        events = list(_provider(device).fetch_punches())
    assert [(e.emp_code, e.punch_time_local, e.direction) for e in events] == [
        ("1001", datetime(2026, 9, 1, 8, 0), True),
        ("1002", datetime(2026, 9, 1, 17, 30), False),
    ]
    assert {e.raw["record_size"] for e in events} == {16}


def test_fetch_punches_maps_the_8_byte_layouts_uids_through_the_user_table():
    """The 8-byte layout records the device's internal uid, not the user id —
    the same number only by coincidence. Mapped through the user table the
    way pyzk does, with the uid itself as the fallback for an unknown one."""
    users = [_encode_user_record(7, "EMP-A", "Ann"), _encode_user_record(9, "EMP-B", "Bo")]
    records = [
        _encode_attlog_record_8(7, datetime(2026, 9, 1, 8, 0)),
        _encode_attlog_record_8(9, datetime(2026, 9, 1, 8, 5)),
        _encode_attlog_record_8(42, datetime(2026, 9, 1, 8, 10)),  # no such user
    ]
    with FakeZKDevice(attlog_records=records, user_records=users) as device:
        events = list(_provider(device).fetch_punches())
    assert [e.emp_code for e in events] == ["EMP-A", "EMP-B", "42"]
    assert {e.raw["record_size"] for e in events} == {8}


def test_fetch_employees_reads_the_28_byte_layout():
    users = [_encode_user_record_28(1, 1001, "Ann"), _encode_user_record_28(2, 1002, "Bo")]
    with FakeZKDevice(user_records=users) as device:
        employees = list(_provider(device).fetch_employees())
    assert [(e.external_id, e.emp_code, e.first_name) for e in employees] == [
        ("1", "1001", "Ann"), ("2", "1002", "Bo"),
    ]


def test_split_table_strips_the_size_prefix_and_infers_the_layout():
    body = b"".join(_encode_attlog_record_16(i, datetime(2026, 1, 1)) for i in range(3))
    size, records = _split_table(struct.pack("<I", len(body)) + body, 3, (8, 16, 40), "t")
    assert size == 16 and records == [body[i:i + 16] for i in (0, 16, 32)]
    # No count from the device, or one that fits no layout: largest layout.
    assert _split_table(struct.pack("<I", 40) + bytes(40), None, (8, 16, 40), "t")[0] == 40
    assert _split_table(struct.pack("<I", 36) + bytes(36), 3, (8, 16, 40), "t")[0] == 40
    # A count of zero is empty, whatever follows.
    assert _split_table(struct.pack("<I", 40) + bytes(40), 0, (8, 16, 40), "t")[1] == []


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


class _SilentDevice:
    """Accepts a TCP connection and then never sends anything back —
    reachable, unlike test_connect_refused_... above, but hung: the real
    shape of a wrong comm key, an unsupported protocol variant, or a
    middlebox that accepts the connection and drops what's sent over it."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self._accepted: socket.socket | None = None
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        try:
            self._accepted, _addr = self._sock.accept()
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        if self._accepted is not None:
            try:
                self._accepted.close()
            except OSError:
                pass
        self._thread.join(timeout=5)

    def __enter__(self) -> "_SilentDevice":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_a_reachable_but_silent_device_raises_a_clear_timeout_error():
    """Regression for a real customer report: a device that accepts the TCP
    connection (so nothing in _open()'s own connect-phase error handling
    ever fires) but never answers the CMD_CONNECT handshake used to escape
    as a raw, unhandled TimeoutError — past every ``except ProviderError``
    the app catches (ZKError is one; a bare TimeoutError is not), reaching
    the customer as an opaque HTTP 500 instead of a message that says what
    to check. _Connection.timeout is passed explicitly and short here so
    the test doesn't sit through the real 15s SOCKET_TIMEOUT."""
    with _SilentDevice() as device:
        conn = _Connection(device.host, device.port, comm_key=0, timeout=0.3)
        with pytest.raises(ZKError, match="Timed out waiting for a reply"):
            with conn:
                pass


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


def test_create_employee_rejects_codes_longer_than_the_user_id_field():
    with FakeZKDevice(user_records=[]) as device:
        provider = _provider(device)
        with pytest.raises(ZKError, match="at most 24"):
            provider.create_employee(EmployeeRecord(external_id=None, emp_code="X" * 25))


def test_a_24_character_code_round_trips_through_provisioning_and_punches():
    """The old 9-character cap came from reading 9 bytes of what is a
    24-byte user id field in the 40-byte attendance record — see _ATTLOG_40."""
    code = "EMP-2026-ENGINEERING-001"
    assert len(code) == 24
    with FakeZKDevice(user_records=[]) as device:
        provider = _provider(device)
        provider.create_employee(EmployeeRecord(external_id=None, emp_code=code))
        assert [u.emp_code for u in provider.fetch_employees()] == [code]
        device.attlog_records = [_encode_attlog_record(code, datetime(2026, 9, 1, 9, 0))]
        assert [p.emp_code for p in provider.fetch_punches()] == [code]


def test_create_employee_on_a_28_byte_user_table_writes_that_layout():
    """ZK6-style firmware stores the user id as a 32-bit number, so the
    record written back has to be the 28-byte layout, carrying the code as
    a number — and a code that isn't one is refused rather than mangled."""
    with FakeZKDevice(user_records=[_encode_user_record_28(1, 1001, "Ann")]) as device:
        provider = _provider(device)
        created = provider.create_employee(EmployeeRecord(external_id=None, emp_code="1002"))
        assert created.external_id == "2"
        assert len(device.user_records[-1]) == 28
        assert sorted(u.emp_code for u in provider.fetch_employees()) == ["1001", "1002"]
        for bad in ("EMP7", "0042"):
            with pytest.raises(ZKError, match="plain numbers"):
                provider.create_employee(EmployeeRecord(external_id=None, emp_code=bad))


def test_create_employee_rejects_blank_emp_code():
    with FakeZKDevice(user_records=[]) as device:
        provider = _provider(device)
        with pytest.raises(ZKError):
            provider.create_employee(EmployeeRecord(external_id=None, emp_code="   "))
