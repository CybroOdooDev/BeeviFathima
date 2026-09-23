"""Standalone ZKTeco terminals as an AttendanceProvider — no BioTime server.

This talks the raw TCP/IP protocol these terminals themselves speak on
port 4370 (the same protocol BioTime's own agent uses to reach a terminal,
and the one every third-party "ZK attendance" tool implements). It is not a
published API — ZKTeco has never released a formal spec — but it has been
independently reverse-engineered and documented identically by many
unrelated projects over the years, which is the only reason it is safe to
rely on at all. This implementation follows that public documentation
(https://github.com/adrobinoga/zk-protocol) rather than any vendor SDK.

What this deliberately does NOT do
-----------------------------------
Some terminals are configured with a "comm key" (a numeric password). The
handshake for that case (``CMD_AUTH``) scrambles the key using a function
tied to the session id that no public write-up has actually reproduced —
every source that mentions it says the same thing: nobody has documented
it. Guessing at a security handshake is worse than refusing it outright, so
a device that demands one gets a clear, specific error instead of a
best-effort auth attempt that would either fail confusingly or, worse,
silently misbehave. Set the device's comm key back to 0 (the factory
default) to connect it here.

This has not been exercised against a physical terminal in this
environment — there is no hardware to test against. The wire framing,
checksum and record layout below follow the documented spec exactly and are
covered by protocol-level tests against a fake device speaking the same
bytes, but that is not the same guarantee as a real unit's firmware. Pilot
one device before relying on this for payroll.

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

CMD_ACK_OK = 2000
CMD_ACK_ERROR = 2001
CMD_ACK_DATA = 2002
CMD_ACK_UNAUTH = 2005

#: verify_state -> our normalised direction. See data-record.md: 0/3/4 are the
#: "in" family (check-in, break-in, overtime-in), 1/2/5 are "out".
_STATE_IN = {0, 3, 4}
_STATE_OUT = {1, 2, 5}
_VERIFY_TYPE_LABEL = {0: "password", 1: "fingerprint", 2: "card"}

_ATTLOG_RECORD_SIZE = 40
#: uid(H) + privilege(B) + password(8s) + name(24s) + card_number(I) +
#: pad(x) + group_id(7s) + pad(x) + user_id(24s) — verified against pyzk's
#: own struct formats, not re-derived from the written spec (an earlier pass
#: over the docs alone got group_id and user_id's widths wrong; see
#: ``_USER_STRUCT``).
_USER_RECORD_SIZE = 72
_USER_STRUCT = "<HB8s24sIx7sx24s"
#: The attendance-log record (see ``_parse_record`` below) only carries 9
#: bytes of user id, even though the live user table allows 24. An employee
#: code provisioned longer than this would enroll fine but every future
#: punch from that person would come back truncated and never match back —
#: a silent mapping failure. ``create_employee`` refuses anything longer.
_MAX_PROVISIONABLE_CODE_LEN = 9
#: Largest single chunk requested per ``_CMD_READ_BUFFER`` call over TCP.
_MAX_CHUNK = 0xFFC0


class ZKError(ProviderError):
    """Any failure talking to a standalone ZKTeco terminal."""


class ZKAuthError(ZKError):
    """The terminal has a comm key set — see the module docstring."""


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
        self._reply_id = 0
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
        if reply.command == CMD_ACK_UNAUTH:
            raise ZKAuthError(
                "This device has a communication password (comm key) set. "
                "BioBridge cannot authenticate against one yet — set the "
                "device's comm key back to 0 to connect it, or leave it "
                "disconnected until that is supported."
            )
        if reply.command != CMD_ACK_OK:
            raise ZKError(
                f"The device refused the connection (reply code {reply.command})."
            )
        # _exchange() already advanced the reply-id counter past this first
        # round trip; only the session id (assigned by the device) is new.
        self.session_id = reply.session_id

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
        assert self.sock is not None
        header = struct.pack("<HHHH", command, 0, self.session_id, self._reply_id)
        body = header + data
        checksum = _checksum16(body)
        header = struct.pack("<HHHH", command, checksum, self.session_id, self._reply_id)
        body = header + data
        packet = _MAGIC + struct.pack("<I", len(body)) + body
        self.sock.sendall(packet)

    def _recv(self) -> _Reply:
        assert self.sock is not None
        outer = _recv_exact(self.sock, 8)
        if outer[:4] != _MAGIC:
            raise ZKError("The device sent a reply with no recognisable framing.")
        (length,) = struct.unpack("<I", outer[4:8])
        if length < 8:
            raise ZKError(f"The device sent an impossibly short reply ({length} bytes).")
        body = _recv_exact(self.sock, length)
        command, _checksum, session_id, reply_id = struct.unpack("<HHHH", body[:8])
        return _Reply(command=command, session_id=session_id, reply_id=reply_id, data=body[8:])

    def _exchange(self, command: int, data: bytes) -> _Reply:
        self._send(command, data)
        reply = self._recv()
        self._reply_id += 1
        return reply

    # -- commands --------------------------------------------------------
    def get_param(self, name: str) -> str | None:
        reply = self._exchange(CMD_OPTIONS_RRQ, f"{name}\x00".encode("ascii", "ignore"))
        if reply.command != CMD_ACK_OK:
            return None
        text = reply.data.split(b"\x00", 1)[0].decode("ascii", "ignore")
        _, _, value = text.partition("=")
        return value.strip() or None

    def get_attlog_count(self) -> int | None:
        self._exchange(CMD_DISABLEDEVICE, b"")
        try:
            reply = self._exchange(CMD_GET_FREE_SIZES, b"")
        finally:
            self._exchange(CMD_ENABLEDEVICE, b"")
        if reply.command != CMD_ACK_OK or len(reply.data) < 36:
            return None
        (count,) = struct.unpack("<I", reply.data[32:36])
        return count

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
        with self._connect() as conn:
            serial = self._serial(conn)
            buffer = conn.read_with_buffer(CMD_ATTLOG_RRQ)

        usable = len(buffer) - (len(buffer) % _ATTLOG_RECORD_SIZE)
        if usable != len(buffer):
            log.warning(
                "ZK device %s: attendance buffer is %d bytes, not a multiple of "
                "%d — dropping the trailing %d byte(s).",
                serial, len(buffer), _ATTLOG_RECORD_SIZE, len(buffer) - usable,
            )

        for offset in range(0, usable, _ATTLOG_RECORD_SIZE):
            record = buffer[offset:offset + _ATTLOG_RECORD_SIZE]
            event = self._parse_record(record, serial)
            if event is None:
                continue
            if since is not None and event.punch_time_local < since:
                continue
            if until is not None and event.punch_time_local > until:
                continue
            yield event

    @staticmethod
    def _parse_record(record: bytes, terminal_sn: str) -> PunchEvent | None:
        user_id = record[2:11].split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
        if not user_id:
            return None
        verify_type = record[26]
        (enc_time,) = struct.unpack("<I", record[27:31])
        verify_state = record[31]
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
            },
        )

    @staticmethod
    def _parse_user_record(record: bytes) -> EmployeeRecord | None:
        if len(record) < _USER_RECORD_SIZE:
            return None
        uid, privilege, _password, name, card_number, group_id, user_id_raw = struct.unpack(
            _USER_STRUCT, record[:_USER_RECORD_SIZE]
        )
        emp_code = user_id_raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
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
                "card_number": card_number,
                "group_id": group_id.split(b"\x00", 1)[0].decode("ascii", "ignore").strip(),
                "full_name": full_name,
            },
        )

    # -- optional ------------------------------------------------------
    def fetch_employees(self) -> Iterator[EmployeeRecord]:
        with self._connect() as conn:
            buffer = conn.read_with_buffer(CMD_USERTEMP_RRQ, FCT_USER)

        usable = len(buffer) - (len(buffer) % _USER_RECORD_SIZE)
        if usable != len(buffer):
            log.warning(
                "ZK device: user table is %d bytes, not a multiple of %d — "
                "dropping the trailing %d byte(s).",
                len(buffer), _USER_RECORD_SIZE, len(buffer) - usable,
            )
        for offset in range(0, usable, _USER_RECORD_SIZE):
            record = self._parse_user_record(buffer[offset:offset + _USER_RECORD_SIZE])
            if record is not None:
                yield record

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
        if len(emp_code) > _MAX_PROVISIONABLE_CODE_LEN:
            raise ZKError(
                f"'{emp_code}' is {len(emp_code)} characters, but this device's "
                f"attendance log only records the first {_MAX_PROVISIONABLE_CODE_LEN} "
                "characters of a user id. Provisioning it would enroll fine, but "
                "every future punch from this person would come back truncated "
                "and never match back to them — so BioBridge refuses. Use an "
                f"employee code of {_MAX_PROVISIONABLE_CODE_LEN} characters or "
                "fewer for devices on this protocol."
            )

        name = (record.full_name or emp_code).encode("utf-8", "ignore")[:24]
        with self._connect() as conn:
            buffer = conn.read_with_buffer(CMD_USERTEMP_RRQ, FCT_USER)
            usable = len(buffer) - (len(buffer) % _USER_RECORD_SIZE)

            existing_uids: list[int] = []
            for offset in range(0, usable, _USER_RECORD_SIZE):
                chunk = buffer[offset:offset + _USER_RECORD_SIZE]
                existing = self._parse_user_record(chunk)
                if existing is None:
                    continue
                existing_uids.append(existing.raw["uid"])
                if existing.emp_code == emp_code:
                    # Already provisioned under this code — nothing to do.
                    # Not an error: reconciliation runs are expected to call
                    # this again for people already handled on a prior run.
                    return existing

            uid = max(existing_uids, default=0) + 1
            payload = struct.pack(
                _USER_STRUCT,
                uid,
                0,   # privilege: always an ordinary user, never admin
                b"",  # password: none — verification stays biometric/PIN on the device
                name,
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
