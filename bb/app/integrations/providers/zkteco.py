"""Standalone ZKTeco terminals as an AttendanceProvider — no BioTime server.

This talks the raw TCP/IP protocol these terminals themselves speak on
port 4370 (the same protocol BioTime's own agent uses to reach a terminal,
and the one every third-party "ZK attendance" tool implements). It is not a
published API — ZKTeco has never released a formal spec — but it has been
independently reverse-engineered and documented identically by many
unrelated projects over the years, which is the only reason it is safe to
rely on at all. This implementation follows that public documentation
(https://github.com/adrobinoga/zk-protocol) rather than any vendor SDK.

Comm key 0 still needs CMD_AUTH
---------------------------------
A terminal can answer ``CMD_CONNECT`` with ``CMD_ACK_UNAUTH``, asking the
client to authenticate with ``CMD_AUTH`` and its comm key (a numeric
password) scrambled with the session id. Many terminals ask for this even
with the comm key left at its factory default of 0 — the real one this was
piloted against did. The scramble is ``_make_commkey``, a line-for-line
port of ``pyzk``'s (from ZKTeco's ``commpro.c``), pinned against its output
in the tests.

An earlier version of this module refused ``CMD_ACK_UNAUTH`` outright,
believing the scramble undocumented. It isn't, and refusing it locked out
every terminal that asks — including ones whose key is 0 — with an error
telling the user to set a key that was already 0.

This has not been exercised against a physical terminal in this
environment — there is no hardware to test against here. The wire framing,
checksum and record layout below are covered by protocol-level tests
against a fake device, and the first two packets of a session are pinned
byte-for-byte to what ``pyzk`` sends (see tests/test_zkteco_protocol.py),
but that is not a substitute for piloting your own hardware before relying
on this for payroll — firmware varies.

The reply-id checksum rule (found against a real terminal)
-----------------------------------------------------------
A real terminal (comm key unset, correct port, ADMS off) accepted the TCP
connection from this client and then never answered ``CMD_CONNECT`` — a
full timeout — while ``pyzk`` connected to the same device instantly.
Diffing the two clients' CMD_CONNECT packets byte for byte found exactly
one difference, in the checksum: ``pyzk`` computes it over the header
carrying the *previous* reply id and sends the header carrying the *next*
one; this client computed it over the id it actually sent. The checksum
algorithm itself was already identical. The device evidently validates
``pyzk``'s form and silently drops anything else, so every packet this
client had ever sent a real terminal was being ignored. The fake device in
the tests never checked checksums, which is how it went unnoticed; it now
validates them the same way. See ``_Connection._send``.

The table layouts (found against the same terminal)
----------------------------------------------------
With the connection working, sync still brought nothing in. The attendance
log and the user table both arrive as a 4-byte total-size prefix followed by
the records, and this module had been parsing from byte 0 — reading every
record 4 bytes out of line, so timestamps decoded as garbage and records
were skipped. It also assumed one layout each (40-byte attendance, 72-byte
users), when firmware uses 8/16/40 and 28/72 — the size given by nothing
but total size / record count. And the 40-byte record's user id is 24 bytes,
not the 9 this read (the basis of an old 9-character cap on provisioned
employee codes, now removed). All of it is pyzk's get_attendance/get_users/
set_user; the parser was checked against pyzk's own on every layout
combination. The fake device had sent tables with no prefix, which is how
none of this showed up in tests; it now sends them as real devices do.

(An intermediate attempt before that diff added a throwaway "warm-up" TCP
connection ahead of the real one, on the theory that ``pyzk``'s preliminary
probe connect was what made the difference. It didn't help and was
removed — noted here so nobody reintroduces it on the same reasoning.)

A note on a bug that lived here briefly
----------------------------------------
An earlier version of this module's checksum used a plain "sum, fold once,
XOR 0xFFFF" finalization. That is the textbook one's-complement checksum,
but it is NOT what these terminals actually compute (copied by every
independent implementation, including ``pyzk``'s, from ZKTeco's own
``zkemsdk.c``): the real finalization is a signed two's-complement
negation with a specific re-fold, and it disagrees with the textbook
version on every packet tested. It went unnoticed because the fake test
device validated against the same wrong formula — internally consistent,
not protocol-correct — so real firmware would have rejected every packet.
Fixed below, cross-checked against ``pyzk`` (the reference implementation
actually run against hardware) rather than re-derived from the written
spec a second time.

The chunked "buffered read" used for both the attendance log and the user
list below was corrected the same way, for the same reason: the documented
shape (send the real request directly, get ``CMD_PREPARE_DATA``/``CMD_DATA``
straight back) is not what real firmware speaks either. Every buffered
read actually goes through an outer ``CMD_PREPARE_BUFFER`` request whose
payload names the real command, with ``CMD_PREPARE_DATA``/``CMD_DATA``
appearing one level deeper, per chunk. See ``_Connection.read_with_buffer``.
"""

from __future__ import annotations

import logging
import socket
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from app.integrations.base import (
    AttendanceProvider,
    Capability,
    ConnectionInfo,
    EmployeeRecord,
    ProviderError,
    PunchEvent,
    SourceConfig,
    TerminalRecord,
    register,
)

log = logging.getLogger(__name__)

DEFAULT_PORT = 4370
#: The terminal only tolerates one live session; get in, read, get out.
SOCKET_TIMEOUT = 15

_MAGIC = b"\x50\x50\x82\x7d"  # "PP\x82}" — every TCP packet starts with this.
#: Reply ids wrap modulo 65535 (USHRT_MAX), not 65536 — pyzk's own wrap.
_REPLY_ID_CYCLE = 65535

# -- command codes (see module docstring for the spec this follows) ---------
CMD_CONNECT = 1000
CMD_EXIT = 1001
CMD_ENABLEDEVICE = 1002
CMD_DISABLEDEVICE = 1003
CMD_PREPARE_DATA = 1500
CMD_DATA = 1501
CMD_FREE_DATA = 1502
CMD_OPTIONS_RRQ = 11
CMD_ATTLOG_RRQ = 13
CMD_GET_FREE_SIZES = 50
CMD_USER_WRQ = 8
CMD_USERTEMP_RRQ = 9
CMD_REFRESHDATA = 1013
FCT_USER = 5
#: The true outer command for any chunked "buffered" read (attendance log,
#: user table, ...) — see ``_Connection.read_with_buffer``. Leading
#: underscore: these are transport plumbing, never a request in their own
#: right, unlike CMD_ATTLOG_RRQ/CMD_USERTEMP_RRQ above which name *what* to
#: read and get passed as a parameter to it.
_CMD_PREPARE_BUFFER = 1503
_CMD_READ_BUFFER = 1504

CMD_AUTH = 1102
CMD_ACK_OK = 2000
CMD_ACK_ERROR = 2001
CMD_ACK_DATA = 2002
CMD_ACK_UNAUTH = 2005

#: verify_state -> our normalised direction. See data-record.md: 0/3/4 are the
#: "in" family (check-in, break-in, overtime-in), 1/2/5 are "out".
_STATE_IN = {0, 3, 4}
_STATE_OUT = {1, 2, 5}
_VERIFY_TYPE_LABEL = {0: "password", 1: "fingerprint", 2: "card"}

# -- table layouts ----------------------------------------------------------
# Both the attendance log and the user table arrive as a 4-byte total-size
# prefix followed by fixed-size records — but the record size depends on the
# firmware, and nothing in the table says which. As pyzk does, it's worked
# out as total size / record count (the count from CMD_GET_FREE_SIZES), with
# the largest layout as the fallback. Every struct format below is copied
# from pyzk 0.9's get_attendance/get_users/set_user, not re-derived from the
# written spec (see "The table layouts" in the module docstring).
#
# Attendance log — (uid or user id, timestamp, verify type, punch state):
#: 8 bytes: uid(H) verify(B) time(4s) state(B). Carries the device's internal
#: uid, not the user id — mapped back through the user table.
_ATTLOG_8 = "<HB4sB"
#: 16 bytes: user_id(I) time(4s) verify(B) state(B) reserved(2s) workcode(I).
#: The user id is a 32-bit number.
_ATTLOG_16 = "<I4sBB2sI"
#: 40 bytes: uid(H) user_id(24s) verify(B) time(4s) state(B) reserved(8s).
_ATTLOG_40 = "<H24sB4sB8s"
_ATTLOG_SIZES = (8, 16, 40)
#
# User table:
#: 28 bytes ("ZK6"): uid(H) privilege(B) password(5s) name(8s) card(I) pad
#: group(B) timezone(H) user_id(I). The user id is a 32-bit number.
_USER_28 = "<HB5s8sIxBHI"
#: 72 bytes ("ZK8"): uid(H) privilege(B) password(8s) name(24s) card(I) pad
#: group(7s) pad user_id(24s).
_USER_STRUCT = "<HB8s24sIx7sx24s"
_USER_SIZES = (28, 72)
#: Largest single chunk requested per ``_CMD_READ_BUFFER`` call over TCP.
_MAX_CHUNK = 0xFFC0


class ZKError(ProviderError):
    """Any failure talking to a standalone ZKTeco terminal."""


class ZKAuthError(ZKError):
    """The terminal rejected the comm key configured for it."""


def _make_commkey(key: int, session_id: int, ticks: int = 50) -> bytes:
    """Scramble a comm key with the session id, for ``CMD_AUTH``.

    A line-for-line port of ``pyzk``'s ``make_commkey`` (itself from
    ZKTeco's ``commpro.c`` ``MakeKey``): reverse the key's 32 bits, add the
    session id, XOR the four bytes with "ZKSO", swap the two 16-bit halves,
    then XOR with ``ticks`` — whose byte also replaces the third one. Output
    is pinned against pyzk's in tests/test_zkteco_protocol.py.

    Note many terminals demand this handshake even with the key left at the
    factory default of 0 — see "Comm key 0 still needs CMD_AUTH" in the
    module docstring.
    """
    k = 0
    for i in range(32):
        k = (k << 1 | 1) if key & (1 << i) else k << 1
    k += session_id
    b = struct.pack("<I", k & 0xFFFFFFFF)
    b = bytes((b[0] ^ ord("Z"), b[1] ^ ord("K"), b[2] ^ ord("S"), b[3] ^ ord("O")))
    lo, hi = struct.unpack("<HH", b)
    b = struct.pack("<HH", hi, lo)
    t = 0xFF & ticks
    return bytes((b[0] ^ t, b[1] ^ t, t, b[3] ^ t))


def _checksum16(payload: bytes) -> int:
    """The protocol's real "quick checksum".

    This is NOT a textbook one's-complement checksum (sum, fold once, XOR
    0xFFFF) — see the module docstring for how that wrong version ended up
    here originally. The actual algorithm, ported directly from ``pyzk``
    (which is itself a port of ZKTeco's own ``zkemsdk.c``): accumulate
    16-bit LE words one at a time, re-folding into range after *each*
    addition rather than once at the end; then negate (Python's arbitrary-
    precision two's complement, not a 16-bit XOR) and re-fold that back into
    range by repeated subtraction. Both re-folds matter — dropping either
    one reproduces the old, wrong checksum.
    """
    length = len(payload)
    i = 0
    checksum = 0
    while length > 1:
        checksum += payload[i] | (payload[i + 1] << 8)
        if checksum > 0xFFFF:
            checksum -= 0xFFFF
        i += 2
        length -= 2
    if length:
        checksum += payload[i]
    while checksum > 0xFFFF:
        checksum -= 0xFFFF
    checksum = ~checksum
    while checksum < 0:
        checksum += 0xFFFF
    return checksum


def _decode_time(enc_t: int) -> datetime:
    """Reverse the terminal's own simplified-calendar time encoding.

    The device does not use real calendar arithmetic to pack a timestamp —
    it uses fixed 31-day months and 365-day years — so this only round-trips
    correctly against *its own* encoder, not against a real calendar. That
    is by design on the device's side, not a bug here.
    """
    second = enc_t % 60
    enc_t //= 60
    minute = enc_t % 60
    enc_t //= 60
    hour = enc_t % 24
    enc_t //= 24
    day = enc_t % 31 + 1
    enc_t //= 31
    month = enc_t % 12 + 1
    enc_t //= 12
    year = enc_t + 2000
    # A device with a wildly wrong clock (dead battery, never configured)
    # produces a (year, month, day) that plain datetime() rejects outright
    # (e.g. day 31 in a fake "February") — better to say so than to crash
    # the whole fetch over one bad record.
    return datetime(year, month, day, hour, minute, second)


def _split_table(
    buffer: bytes, count: int | None, sizes: tuple[int, ...], what: str
) -> tuple[int, list[bytes]]:
    """Strip a table's 4-byte size prefix and cut it into records.

    Returns ``(record_size, records)``. ``count`` is the record count from
    CMD_GET_FREE_SIZES: 0 means empty (pyzk doesn't even read the table), and
    None means the device didn't say, in which case — as when total/count
    matches no known layout — the largest layout in ``sizes`` is assumed.
    """
    fallback = sizes[-1]
    if count == 0 or len(buffer) < 4:
        return fallback, []
    (total,) = struct.unpack("<I", buffer[:4])
    body = buffer[4:]
    if total != len(body):
        log.warning(
            "ZK device: %s prefix says %d bytes follow, got %d.", what, total, len(body)
        )

    record_size = fallback
    if count:
        per_record = total / count
        if per_record in sizes:
            record_size = int(per_record)
        else:
            log.warning(
                "ZK device: %s is %d bytes for %d record(s) — %.1f each, which "
                "matches no known layout %s. Assuming %d.",
                what, total, count, per_record, sizes, fallback,
            )

    usable = len(body) - (len(body) % record_size)
    if usable != len(body):
        log.warning(
            "ZK device: %s is %d bytes, not a multiple of %d — dropping the "
            "trailing %d byte(s).",
            what, len(body), record_size, len(body) - usable,
        )
    return record_size, [body[i:i + record_size] for i in range(0, usable, record_size)]


def _parse_user(record_size: int, record: bytes) -> EmployeeRecord | None:
    """One user-table record, in either layout (see ``_USER_28``/``_USER_STRUCT``)."""
    if record_size == 28:
        uid, privilege, _password, name, card, group, _tz, user_id = struct.unpack(_USER_28, record)
        emp_code = str(user_id)
        group_id = str(group)
    else:
        uid, privilege, _password, name, card, group, user_id_raw = struct.unpack(
            _USER_STRUCT, record
        )
        emp_code = user_id_raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
        group_id = group.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    if not emp_code:
        return None
    full_name = name.split(b"\x00", 1)[0].decode("utf-8", "ignore").strip()
    first_name, _, last_name = full_name.partition(" ")
    return EmployeeRecord(
        external_id=str(uid),
        emp_code=emp_code,
        first_name=first_name,
        last_name=last_name,
        # The live user table carries no per-user enable/disable flag —
        # that's a separate, unrelated command this doesn't use.
        is_active=True,
        raw={
            "uid": uid,
            "privilege": privilege,
            "card_number": card,
            "group_id": group_id,
            "full_name": full_name,
        },
    )


def _parse_attendance(
    record_size: int, record: bytes, terminal_sn: str, uid_to_code: dict[int, str]
) -> PunchEvent | None:
    """One attendance-log record, in any of the three layouts."""
    if record_size == 8:
        uid, verify_type, raw_time, verify_state = struct.unpack(_ATTLOG_8, record)
        # This layout carries the internal uid; the user table maps it to the
        # user id everything else uses. pyzk falls back to the uid itself.
        user_id = uid_to_code.get(uid, str(uid))
    elif record_size == 16:
        numeric_id, raw_time, verify_type, verify_state, _res, _workcode = struct.unpack(
            _ATTLOG_16, record
        )
        user_id = str(numeric_id)
    else:
        _uid, user_id_raw, verify_type, raw_time, verify_state, _res = struct.unpack(
            _ATTLOG_40, record
        )
        user_id = user_id_raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    if not user_id:
        return None

    (enc_time,) = struct.unpack("<I", raw_time)
    try:
        punch_time = _decode_time(enc_time)
    except ValueError:
        log.warning(
            "ZK device %s: record for user %s has an unreadable timestamp "
            "(%d) — the device's clock is likely unset. Skipping.",
            terminal_sn, user_id, enc_time,
        )
        return None

    direction = True if verify_state in _STATE_IN else False if verify_state in _STATE_OUT else None
    return PunchEvent(
        external_id=f"{user_id}:{enc_time}:{verify_state}",
        emp_code=user_id,
        punch_time_local=punch_time,
        direction=direction,
        terminal_sn=terminal_sn,
        verify_type=_VERIFY_TYPE_LABEL.get(verify_type, str(verify_type)),
        raw={
            "user_id": user_id,
            "verify_type": verify_type,
            "verify_state": verify_state,
            "enc_time": enc_time,
            "record_size": record_size,
        },
    )


def _parse_address(base_url: str) -> tuple[str, int]:
    """``base_url`` holds this source's address, not an HTTP URL.

    Accepts ``zk://host:port``, ``zk://host``, or a bare ``host[:port]`` —
    the settings UI always sends the ``zk://`` form, but a bare host is
    accepted too so a source built by hand or by an older client still
    works.
    """
    value = (base_url or "").strip()
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname
        port = parsed.port or DEFAULT_PORT
    elif ":" in value and value.count(":") == 1:
        host, _, port_str = value.partition(":")
        try:
            port = int(port_str)
        except ValueError:
            raise ZKError(f"'{base_url}' has no valid port after the colon.") from None
    else:
        host, port = value, DEFAULT_PORT
    if not host:
        raise ZKError(f"'{base_url}' does not name a device address.")
    return host, port


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ZKError(
                "The device closed the connection mid-reply — it may have "
                "dropped the session, or another client is already connected "
                "(most of these terminals allow only one at a time)."
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@dataclass
class _Reply:
    command: int
    session_id: int
    reply_id: int
    data: bytes


class _Connection:
    """One short-lived TCP session with one terminal.

    Deliberately not reused across calls: these terminals commonly refuse a
    second simultaneous connection, so every provider method opens its own
    connection, does its one job, and disconnects — never holding a session
    open between sync cycles.
    """

    def __init__(self, host: str, port: int, comm_key: int, timeout: int = SOCKET_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.comm_key = comm_key
        self.timeout = timeout
        self.session_id = 0
        #: The reply id of the *last* exchange, not the next one to send —
        #: see _send for why that distinction is load-bearing. Starts one
        #: step before 0 in the protocol's mod-65535 cycle, so the first
        #: packet (CMD_CONNECT) goes out carrying reply id 0.
        self._reply_id = _REPLY_ID_CYCLE - 1
        self.sock: socket.socket | None = None

    def __enter__(self) -> "_Connection":
        self._open()
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()

    # -- transport -----------------------------------------------------
    def _open(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except socket.gaierror as exc:
            raise ZKError(f"Cannot resolve the device address '{self.host}': {exc}") from exc
        except TimeoutError:
            raise ZKError(
                f"Timed out connecting to {self.host}:{self.port}. The device is "
                "usually up but unreachable — check it is on the same network "
                "or VPN as wherever BioBridge runs, and that nothing firewalls "
                f"port {self.port}."
            ) from None
        except ConnectionRefusedError:
            raise ZKError(
                f"Connection refused by {self.host}:{self.port}. Nothing is "
                "listening there — check the IP address and that network "
                "communication is enabled on the device."
            ) from None
        except OSError as exc:
            raise ZKError(f"Cannot reach {self.host}:{self.port}: {exc}") from exc

        reply = self._exchange(CMD_CONNECT, b"")
        # The device assigns the session id in its CONNECT reply, whatever
        # that reply says — and CMD_AUTH below needs it for both its header
        # and the key scramble, so it's taken before anything else. (_exchange
        # has already taken the reply id the device echoed back.)
        self.session_id = reply.session_id

        if reply.command == CMD_ACK_UNAUTH:
            # Asked to authenticate. Normal even with the comm key at its
            # factory default of 0 — many terminals always ask; see the
            # module docstring. Same handshake pyzk does.
            reply = self._exchange(CMD_AUTH, _make_commkey(self.comm_key, self.session_id))
            if reply.command == CMD_ACK_UNAUTH:
                raise ZKAuthError(
                    "The device rejected the comm key. Check that the Comm key "
                    "configured for this connection matches the one set on the "
                    "device (Comm → Comm Key on most models). Leave it blank "
                    "here if the device's is 0."
                )

        if reply.command != CMD_ACK_OK:
            raise ZKError(
                f"The device refused the connection (reply code {reply.command})."
            )

    def _close(self) -> None:
        if self.sock is None:
            return
        try:
            self._exchange(CMD_EXIT, b"")
        except ZKError:
            pass  # best-effort — the socket is closing either way
        finally:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _send(self, command: int, data: bytes) -> None:
        """Frame and send one request.

        The checksum is computed over the header carrying the *previous*
        reply id (``self._reply_id``), while the header actually sent
        carries the *next* one. That looks like an off-by-one, but it is
        what real terminals validate against: it is exactly what ``pyzk``
        (and the ``zkemsdk.c`` it was ported from) sends, and a terminal
        that got a checksum over the sent reply id instead silently dropped
        the packet — no reply, just a timeout. That was the one byte of
        difference between this client's CMD_CONNECT and ``pyzk``'s against
        a real device that answered ``pyzk`` and ignored this; the module
        docstring has the whole story.
        """
        assert self.sock is not None
        next_reply_id = (self._reply_id + 1) % _REPLY_ID_CYCLE
        header = struct.pack("<HHHH", command, 0, self.session_id, self._reply_id)
        checksum = _checksum16(header + data)
        header = struct.pack("<HHHH", command, checksum, self.session_id, next_reply_id)
        body = header + data
        packet = _MAGIC + struct.pack("<I", len(body)) + body
        try:
            self.sock.sendall(packet)
        except OSError as exc:
            raise self._transport_error(exc) from exc

    def _recv(self) -> _Reply:
        assert self.sock is not None
        # _open()'s own try/except only covers socket.create_connection — the
        # TCP handshake succeeding says nothing about whether the device will
        # actually answer a request once one is sent. Every read after that
        # point can still time out (device is up but never replies — wrong
        # comm key, an unsupported protocol variant, or something between
        # BioBridge and the device quietly eating the response) or drop the
        # connection mid-reply, and neither is a ZKError on its own: it's a
        # raw OSError (TimeoutError/ConnectionResetError/...) that would
        # otherwise propagate straight past every ``except ProviderError``
        # handler above this and surface as an opaque HTTP 500 instead of a
        # message that says what actually went wrong.
        try:
            outer = _recv_exact(self.sock, 8)
        except OSError as exc:
            raise self._transport_error(exc) from exc
        if outer[:4] != _MAGIC:
            raise ZKError("The device sent a reply with no recognisable framing.")
        (length,) = struct.unpack("<I", outer[4:8])
        if length < 8:
            raise ZKError(f"The device sent an impossibly short reply ({length} bytes).")
        try:
            body = _recv_exact(self.sock, length)
        except OSError as exc:
            raise self._transport_error(exc) from exc
        try:
            command, _checksum, session_id, reply_id = struct.unpack("<HHHH", body[:8])
        except struct.error as exc:
            raise ZKError(f"The device's reply header was malformed: {exc}") from exc
        return _Reply(command=command, session_id=session_id, reply_id=reply_id, data=body[8:])

    def _transport_error(self, exc: OSError) -> ZKError:
        if isinstance(exc, TimeoutError):
            return ZKError(
                f"Timed out waiting for a reply from {self.host}:{self.port}. The "
                "device accepted the connection but never answered within "
                f"{self.timeout}s. A terminal silently ignores packets it can't "
                "validate, so this usually means its firmware speaks a protocol "
                "variant this doesn't handle; it can also mean something between "
                "BioBridge and the device is accepting the connection but "
                "dropping what's sent over it. (A wrong comm key is reported "
                "as a rejection, not a timeout.)"
            )
        return ZKError(
            f"Lost the connection to {self.host}:{self.port} mid-request: {exc}"
        )

    def _exchange(self, command: int, data: bytes) -> _Reply:
        self._send(command, data)
        reply = self._recv()
        # Take the id the device echoed rather than counting locally, the
        # same as pyzk: the next request's checksum is computed over it (see
        # _send), so it has to be the value the device itself last used.
        self._reply_id = reply.reply_id
        return reply

    # -- commands --------------------------------------------------------
    def get_param(self, name: str) -> str | None:
        reply = self._exchange(CMD_OPTIONS_RRQ, f"{name}\x00".encode("ascii", "ignore"))
        if reply.command != CMD_ACK_OK:
            return None
        text = reply.data.split(b"\x00", 1)[0].decode("ascii", "ignore")
        _, _, value = text.partition("=")
        return value.strip() or None

    def read_sizes(self) -> tuple[int | None, int | None]:
        """``(user count, attendance record count)``, None where the device
        didn't say. Offsets are pyzk's read_sizes (fields 4 and 8 of 20
        little-endian int32s). The counts are what ``_split_table`` divides
        by to tell which record layout this firmware uses."""
        reply = self._exchange(CMD_GET_FREE_SIZES, b"")
        if reply.command != CMD_ACK_OK:
            return None, None
        data = reply.data
        users = struct.unpack("<i", data[16:20])[0] if len(data) >= 20 else None
        records = struct.unpack("<i", data[32:36])[0] if len(data) >= 36 else None
        return users, records

    def get_attlog_count(self) -> int | None:
        self._exchange(CMD_DISABLEDEVICE, b"")
        try:
            _users, records = self.read_sizes()
        finally:
            self._exchange(CMD_ENABLEDEVICE, b"")
        return records

    def read_users(self) -> tuple[int, list[EmployeeRecord]]:
        """The whole user table, as ``(record_size, users)`` — the record
        size is also the layout any user written back must use."""
        users_count, _records = self.read_sizes()
        if users_count == 0:
            # Nothing to tell the layout from; 72 is pyzk's default too.
            return _USER_SIZES[-1], []
        buffer = self.read_with_buffer(CMD_USERTEMP_RRQ, FCT_USER)
        record_size, records = _split_table(buffer, users_count, _USER_SIZES, "user table")
        parsed = (_parse_user(record_size, r) for r in records)
        return record_size, [u for u in parsed if u is not None]

    def read_with_buffer(self, command: int, fct: int = 0, ext: int = 0) -> bytes:
        """Read a (possibly large) table via the device's chunked-buffer
        transfer — the mechanism behind both the attendance log and the user
        list, and per ``pyzk`` (the hardware-tested reference this was
        cross-checked against) the *only* shape real firmware actually
        speaks, superseding an earlier version here that sent ``command``
        directly at the top level. See the module docstring.

        Every such read is wrapped in an outer ``_CMD_PREPARE_BUFFER``
        request naming the real ``command``/``fct``/``ext``. The device
        replies either with the whole (small) answer directly as
        ``CMD_DATA``, or with an ack whose bytes 1:5 hold a total size,
        which is then pulled down in ``_CMD_READ_BUFFER`` chunks — each
        chunk itself arriving through the same CMD_PREPARE_DATA/CMD_DATA(xN)
        dance a single-shot transfer used, just nested one level deeper.
        """
        self._exchange(CMD_DISABLEDEVICE, b"")
        try:
            request = struct.pack("<bhii", 1, command, fct, ext)
            reply = self._exchange(_CMD_PREPARE_BUFFER, request)

            if reply.command == CMD_DATA:
                return reply.data  # small enough to arrive in one shot

            if reply.command == CMD_ACK_ERROR:
                raise ZKError("The device refused this data request.")

            if reply.command != CMD_ACK_OK:
                raise ZKError(
                    f"Unexpected reply preparing a buffered read (code {reply.command})."
                )
            if len(reply.data) < 5:
                return b""  # nothing to read — e.g. an empty table
            (size,) = struct.unpack("<I", reply.data[1:5])
            if size <= 0:
                return b""

            buffer = bytearray()
            start = 0
            remaining = size
            while remaining > 0:
                chunk_size = min(remaining, _MAX_CHUNK)
                buffer += self._read_chunk(start, chunk_size)
                start += chunk_size
                remaining -= chunk_size

            trailing = self._exchange(CMD_FREE_DATA, b"")
            if trailing.command != CMD_ACK_OK:
                log.debug(
                    "ZK device: unexpected reply %s freeing the buffer",
                    trailing.command,
                )
            return bytes(buffer[:size])
        finally:
            self._exchange(CMD_ENABLEDEVICE, b"")

    def _read_chunk(self, start: int, size: int) -> bytes:
        reply = self._exchange(_CMD_READ_BUFFER, struct.pack("<ii", start, size))

        if reply.command == CMD_DATA:
            return reply.data

        if reply.command == CMD_PREPARE_DATA:
            if len(reply.data) < 4:
                raise ZKError("The device announced a chunk transfer with no size.")
            (expected,) = struct.unpack("<I", reply.data[:4])
            chunk = bytearray()
            # These arrive unsolicited, same as in the top-level single-shot
            # case — the outgoing reply-id counter is untouched here.
            while len(chunk) < expected:
                piece = self._recv()
                if piece.command != CMD_DATA:
                    raise ZKError(
                        f"Expected a data chunk from the device, got reply "
                        f"code {piece.command} instead."
                    )
                chunk += piece.data
            trailing = self._recv()
            if trailing.command not in (CMD_ACK_OK, CMD_ACK_DATA):
                log.debug(
                    "ZK device: unexpected trailing reply %s after a chunk transfer",
                    trailing.command,
                )
            return bytes(chunk[:expected])

        raise ZKError(f"Unexpected reply reading a data chunk (code {reply.command}).")

    def create_user(self, payload: bytes) -> None:
        """Write one 72-byte user record (``_USER_STRUCT``) to the device.

        Identity only — see the module docstring on templates: this cannot
        enroll a fingerprint or face, only a uid/name/id the person can then
        clock with a PIN or card, or have their biometric added locally by
        someone standing at the terminal.
        """
        self._exchange(CMD_DISABLEDEVICE, b"")
        try:
            reply = self._exchange(CMD_USER_WRQ, payload)
            if reply.command != CMD_ACK_OK:
                raise ZKError(
                    f"The device refused to create the user (reply code {reply.command})."
                )
            refreshed = self._exchange(CMD_REFRESHDATA, b"")
            if refreshed.command != CMD_ACK_OK:
                log.debug(
                    "ZK device: unexpected reply %s refreshing data after user create",
                    refreshed.command,
                )
        finally:
            self._exchange(CMD_ENABLEDEVICE, b"")


@register
class ZKDeviceProvider(AttendanceProvider):
    slug = "zk_device"
    label = "Standalone device (ZKTeco protocol)"
    description = (
        "One physical ZKTeco terminal, reached directly over the network — "
        "no BioTime or other server in between."
    )
    capabilities = frozenset({
        Capability.READ_PUNCHES,
        Capability.LIST_TERMINALS,
        Capability.READ_EMPLOYEES,
        Capability.WRITE_EMPLOYEES,
    })
    kinds = frozenset({"device"})
    config_fields = (
        {"name": "base_url", "label": "Device address", "type": "text", "required": True,
         "help": f"The device's own IP, reachable from wherever BioBridge runs. "
                 f"A port is optional — defaults to {DEFAULT_PORT}."},
        {"name": "password", "label": "Comm key", "type": "password", "required": False,
         "help": "Only if the device has a communication password set. Leave "
                 "blank for the factory default (no password)."},
        {"name": "server_timezone", "label": "Device timezone", "type": "timezone",
         "required": True, "default": "UTC",
         "help": "The zone the device itself runs in. Punch times arrive with "
                 "no offset, so a wrong value shifts every attendance record."},
    )

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self.host, self.port = _parse_address(config.base_url)
        raw_key = (config.password or "").strip()
        if raw_key and not raw_key.isdigit():
            raise ZKError("Comm key must be numeric (the device's own PIN-style password).")
        self.comm_key = int(raw_key) if raw_key else 0
        self._serial_cache: str | None = None

    def close(self) -> None:
        pass  # nothing kept open between calls — see _Connection's docstring

    def _connect(self) -> _Connection:
        return _Connection(self.host, self.port, self.comm_key)

    def _serial(self, conn: _Connection) -> str:
        if self._serial_cache is None:
            self._serial_cache = conn.get_param("~SerialNumber") or f"ip:{self.host}"
        return self._serial_cache

    # -- required ------------------------------------------------------
    def test_connection(self) -> ConnectionInfo:
        try:
            with self._connect() as conn:
                serial = self._serial(conn)
                count = conn.get_attlog_count()
        except ZKError as exc:
            return ConnectionInfo(ok=False, message=str(exc))

        message = f"Connected to ZKTeco device {serial}"
        if count is not None:
            message += f" — {count} attendance record(s) on the device"
        return ConnectionInfo(ok=True, message=message, detail={"serial_number": serial, "attlog_count": count})

    def fetch_punches(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> Iterator[PunchEvent]:
        # Everything is read inside one short session and parsed after it
        # closes — never holding the device's only connection slot open while
        # a caller consumes the generator.
        uid_to_code: dict[int, str] = {}
        with self._connect() as conn:
            serial = self._serial(conn)
            _users, records_count = conn.read_sizes()
            if records_count == 0:
                return
            buffer = conn.read_with_buffer(CMD_ATTLOG_RRQ)
            record_size, records = _split_table(
                buffer, records_count, _ATTLOG_SIZES, "attendance log"
            )
            if record_size == 8 and records:
                # This layout carries uids, not user ids — see _ATTLOG_8.
                _usize, users = conn.read_users()
                uid_to_code = {u.raw["uid"]: u.emp_code for u in users}

        for record in records:
            event = _parse_attendance(record_size, record, serial, uid_to_code)
            if event is None:
                continue
            if since is not None and event.punch_time_local < since:
                continue
            if until is not None and event.punch_time_local > until:
                continue
            yield event

    # -- optional ------------------------------------------------------
    def fetch_employees(self) -> Iterator[EmployeeRecord]:
        with self._connect() as conn:
            _size, users = conn.read_users()
        yield from users

    def create_employee(self, record: EmployeeRecord) -> EmployeeRecord:
        """Provision identity only — never a fingerprint or face template.

        See the module docstring: no vendor lets a template be pushed
        remotely, so this creates the device's user-table row (id, name,
        privilege) the person can then clock in with a PIN or card, or have
        their biometric added locally by someone at the terminal. Existing
        users are read first, both to reuse the device's own ``uid`` slot
        scheme and to avoid creating a second row for an ``emp_code``
        already provisioned.
        """
        emp_code = (record.emp_code or "").strip()
        if not emp_code:
            raise ZKError("Cannot provision a device user with no employee code.")

        full_name = record.full_name or emp_code
        with self._connect() as conn:
            record_size, users = conn.read_users()
            for existing in users:
                if existing.emp_code == emp_code:
                    # Already provisioned under this code — nothing to do.
                    # Not an error: reconciliation runs are expected to call
                    # this again for people already handled on a prior run.
                    return existing

            # The code has to come back out of the attendance log exactly as
            # it went in, or this person's punches never match them.
            if record_size == 28:
                # This layout stores the user id as a 32-bit number.
                if not (emp_code.isdigit() and str(int(emp_code)) == emp_code
                        and int(emp_code) <= 0xFFFFFFFF):
                    raise ZKError(
                        f"'{emp_code}' can't be provisioned on this device: its "
                        "firmware stores user ids as plain numbers, so only an "
                        "employee code made of digits (no leading zeros) would "
                        "come back unchanged in its punches."
                    )
            elif len(emp_code) > 24:
                raise ZKError(
                    f"'{emp_code}' is {len(emp_code)} characters; this device "
                    "stores at most 24 for a user id."
                )

            uid = max((u.raw["uid"] for u in users), default=0) + 1
            if record_size == 28:
                payload = struct.pack(
                    _USER_28,
                    uid,
                    0,  # privilege: always an ordinary user, never admin
                    b"",  # password: none — verification stays on the device
                    full_name.encode("utf-8", "ignore")[:8],
                    0,  # card: none provisioned remotely
                    0,  # group
                    0,  # timezone
                    int(emp_code),
                )
            else:
                payload = struct.pack(
                    _USER_STRUCT,
                    uid,
                    0,   # privilege: always an ordinary user, never admin
                    b"",  # password: none — verification stays biometric/PIN on the device
                    full_name.encode("utf-8", "ignore")[:24],
                    0,   # card_number: no card provisioned remotely
                    b"",  # group_id: unused
                    emp_code.encode("ascii", "ignore")[:24],
                )
            conn.create_user(payload)

        return EmployeeRecord(
            external_id=str(uid),
            emp_code=emp_code,
            first_name=record.first_name,
            last_name=record.last_name,
            is_active=True,
            raw={"uid": uid, "provisioned_by": "biobridge"},
        )

    def fetch_terminals(self) -> Iterator[TerminalRecord]:
        with self._connect() as conn:
            serial = self._serial(conn)
            name = conn.get_param("~DeviceName")
        yield TerminalRecord(
            serial_number=serial,
            alias=name,
            ip_address=self.host,
            raw={"host": self.host, "port": self.port},
        )
