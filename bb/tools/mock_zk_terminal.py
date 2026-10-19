#!/usr/bin/env python3
"""A simulated standalone ZKTeco terminal, so a direct-device connection works with no hardware.

It speaks the same TCP protocol BioBridge's "Standalone device (ZKTeco protocol)"
connection speaks (port 4370 by default): connect + comm-key handshake, serial
number and device name, the user table, the attendance log, and writing a user.
Point a connection at ``zk://127.0.0.1:4370`` and Test Connection / Sync Now work
as they would against the real unit.

A standalone device is ONE terminal in ONE company, so this takes both:

    python3 tools/mock_zk_terminal.py --tenant gulf-steel --company 1
    python3 tools/mock_zk_terminal.py --tenant gulf-steel --company 2 --port 4371 --control-port 4381

* ``--tenant`` (required) scopes everything to one BioBridge account, exactly as
  tools/mock_biotime.py does. The device's users are that company's Odoo
  employees (those with a Badge ID / PIN / registration number); its serial
  number carries the tenant, e.g. MOCK-GULF-STEEL-C1-ZK-01.
* Live punches are ON by default: every --interval seconds a random enrolled
  user clocks in or out *now*, alternating, like people walking past the unit.
  --no-simulate turns that off.
* The log lives in mock_data/<tenant>/zk_<serial>.json, so a restart keeps the
  device's history, as a real unit would.

Control it over HTTP (on --control-port, default port + 1000, loopback only):

    curl localhost:5370/state                                        # who is in, log size
    curl -X POST localhost:5370/punch -d '{"emp_code": "5"}'          # toggles in/out
    curl -X POST localhost:5370/punch -d '{"emp_code": "5", "state": "out"}'
    curl -X POST localhost:5370/offline    # drop every connection: BioBridge sees the unit as down
    curl -X POST localhost:5370/online
    curl -X POST localhost:5370/clear-log  # the unit's log is wiped (users stay)

Layouts: the default is a current firmware (72-byte users, 40-byte log records,
alphanumeric ids). ``--layout legacy`` imitates old firmware (28-byte users, 8-byte
log records, numeric ids only), which BioBridge also supports.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import socketserver
import struct
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.integrations.providers import zkteco as zk  # noqa: E402  (the protocol constants)

DEVICE_NAME = "MockFace/ID"
VERIFY_FINGERPRINT = 1
STATE_IN, STATE_OUT = 0, 1


# --------------------------------------------------------------------------- #
# The device's state
# --------------------------------------------------------------------------- #
class Device:
    def __init__(self, serial: str, tz: str, layout: str, comm_key: int, log_path: Path | None) -> None:
        self.serial = serial
        self.tz = tz
        self.layout = layout                  # "modern" | "legacy"
        self.comm_key = comm_key
        self.log_path = log_path
        self.lock = threading.RLock()
        self.users: list[dict] = []           # {"uid", "user_id", "name"}
        self.log: list[dict] = []             # {"user_id", "time" (ISO local), "state", "verify"}
        self.offline = False
        self._open: set[socket.socket] = set()
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        if self.log_path and self.log_path.exists():
            data = json.loads(self.log_path.read_text())
            self.log = data.get("log", [])
            self.users = data.get("users", [])

    def save(self) -> None:
        if not self.log_path:
            return
        tmp = self.log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"serial": self.serial, "users": self.users, "log": self.log}, indent=1))
        os.replace(tmp, self.log_path)

    # -- users -------------------------------------------------------------
    def enroll(self, user_id: str, name: str) -> dict | None:
        with self.lock:
            for user in self.users:
                if user["user_id"] == user_id:
                    return user
            if self.layout == "legacy" and not (user_id.isdigit() and str(int(user_id)) == user_id):
                return None                    # old firmware stores a plain number
            user = {"uid": max([u["uid"] for u in self.users] + [0]) + 1, "user_id": user_id, "name": name}
            self.users.append(user)
            self.save()
            return user

    # -- punches -----------------------------------------------------------
    def now_local(self) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(self.tz)).replace(tzinfo=None)

    def last_state(self, user_id: str) -> int | None:
        for rec in reversed(self.log):
            if rec["user_id"] == user_id:
                return rec["state"]
        return None

    def punch(self, user_id: str, state: int | None = None, at: datetime | None = None) -> dict:
        with self.lock:
            if not any(u["user_id"] == user_id for u in self.users):
                raise ValueError(f"user {user_id} is not enrolled on this device")
            if state is None:
                state = STATE_OUT if self.last_state(user_id) == STATE_IN else STATE_IN
            rec = {"user_id": user_id, "time": (at or self.now_local()).strftime("%Y-%m-%d %H:%M:%S"),
                   "state": state, "verify": VERIFY_FINGERPRINT}
            self.log.append(rec)
            self.save()
            return rec

    # -- connections (for /offline) ---------------------------------------
    def track(self, sock: socket.socket) -> None:
        self._open.add(sock)

    def untrack(self, sock: socket.socket) -> None:
        self._open.discard(sock)

    def go_offline(self) -> None:
        self.offline = True
        for sock in list(self._open):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -- wire tables -------------------------------------------------------
    @staticmethod
    def _encode_time(moment: datetime) -> int:
        """The terminal's own simplified-calendar encoding (the inverse of the client's decoder)."""
        return (((moment.year % 100) * 12 * 31 + (moment.month - 1) * 31 + moment.day - 1) * 86400
                + (moment.hour * 60 + moment.minute) * 60 + moment.second)

    def user_table(self) -> bytes:
        rows = []
        for u in self.users:
            if self.layout == "legacy":
                rows.append(struct.pack(zk._USER_28, u["uid"], 0, b"", u["name"].encode()[:8], 0, 0, 0,
                                        int(u["user_id"])))
            else:
                rows.append(struct.pack(zk._USER_STRUCT, u["uid"], 0, b"", u["name"].encode()[:24], 0, b"",
                                        u["user_id"].encode()[:24]))
        return b"".join(rows)

    def attlog_table(self) -> bytes:
        uids = {u["user_id"]: u["uid"] for u in self.users}
        rows = []
        for rec in self.log:
            enc = struct.pack("<I", self._encode_time(datetime.strptime(rec["time"], "%Y-%m-%d %H:%M:%S")))
            if self.layout == "legacy":
                rows.append(struct.pack(zk._ATTLOG_8, uids.get(rec["user_id"], 0), rec["verify"], enc, rec["state"]))
            else:
                rows.append(struct.pack(zk._ATTLOG_40, uids.get(rec["user_id"], 0), rec["user_id"].encode()[:24],
                                        rec["verify"], enc, rec["state"], b""))
        return b"".join(rows)

    def free_sizes(self) -> bytes:
        fields = [0] * 20
        fields[4] = len(self.users)           # user count
        fields[8] = len(self.log)             # attendance record count
        return struct.pack("<20i", *fields)


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #
def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return bytes(data)


class Session(socketserver.BaseRequestHandler):
    device: Device  # set on the class by build_server

    def send(self, command: int, session_id: int, reply_id: int, data: bytes = b"") -> None:
        header = struct.pack("<HHHH", command, 0, session_id, reply_id)
        checksum = zk._checksum16(header + data)
        body = struct.pack("<HHHH", command, checksum, session_id, reply_id) + data
        self.request.sendall(zk._MAGIC + struct.pack("<I", len(body)) + body)

    def handle(self) -> None:
        dev = self.device
        sock = self.request
        if dev.offline:
            return                              # accept, then say nothing: a dead unit
        sock.settimeout(30)
        dev.track(sock)
        session_id = 0
        pending: bytes = b""                    # the table a _CMD_PREPARE_BUFFER announced
        try:
            while True:
                outer = _recv_exact(sock, 8)
                if outer[:4] != zk._MAGIC:
                    return
                (length,) = struct.unpack("<I", outer[4:8])
                body = _recv_exact(sock, length)
                command, _checksum, sid, reply_id = struct.unpack("<HHHH", body[:8])
                data = body[8:]
                if dev.offline:
                    return

                if command == zk.CMD_CONNECT:
                    session_id = random.randint(1, 60000)
                    # Like most real units: always ask for the comm key, even when it is 0.
                    self.send(zk.CMD_ACK_UNAUTH, session_id, reply_id)
                elif command == zk.CMD_AUTH:
                    ok = data == zk._make_commkey(dev.comm_key, session_id)
                    self.send(zk.CMD_ACK_OK if ok else zk.CMD_ACK_UNAUTH, session_id, reply_id)
                elif command == zk.CMD_EXIT:
                    self.send(zk.CMD_ACK_OK, session_id, reply_id)
                    return
                elif command in (zk.CMD_ENABLEDEVICE, zk.CMD_DISABLEDEVICE, zk.CMD_REFRESHDATA, zk.CMD_FREE_DATA):
                    self.send(zk.CMD_ACK_OK, session_id, reply_id)
                elif command == zk.CMD_OPTIONS_RRQ:
                    name = data.split(b"\x00", 1)[0].decode("ascii", "ignore")
                    value = {"~SerialNumber": dev.serial, "~DeviceName": DEVICE_NAME}.get(name)
                    if value is None:
                        self.send(zk.CMD_ACK_ERROR, session_id, reply_id)
                    else:
                        self.send(zk.CMD_ACK_OK, session_id, reply_id, f"{name}={value}\x00".encode())
                elif command == zk.CMD_GET_FREE_SIZES:
                    with dev.lock:
                        self.send(zk.CMD_ACK_OK, session_id, reply_id, dev.free_sizes())
                elif command == zk._CMD_PREPARE_BUFFER:
                    _flag, wanted, fct, _ext = struct.unpack("<bhii", data[:11])
                    with dev.lock:
                        if wanted == zk.CMD_ATTLOG_RRQ:
                            table = dev.attlog_table()
                        elif wanted == zk.CMD_USERTEMP_RRQ and fct == zk.FCT_USER:
                            table = dev.user_table()
                        else:
                            self.send(zk.CMD_ACK_ERROR, session_id, reply_id)
                            continue
                    pending = struct.pack("<I", len(table)) + table      # 4-byte size prefix, then records
                    self.send(zk.CMD_ACK_OK, session_id, reply_id, b"\x00" + struct.pack("<I", len(pending)))
                elif command == zk._CMD_READ_BUFFER:
                    start, size = struct.unpack("<ii", data[:8])
                    chunk = pending[start:start + size]
                    self.send(zk.CMD_PREPARE_DATA, session_id, reply_id, struct.pack("<I", len(chunk)))
                    for i in range(0, len(chunk), 1024):
                        self.send(zk.CMD_DATA, session_id, reply_id, chunk[i:i + 1024])
                    self.send(zk.CMD_ACK_OK, session_id, reply_id)
                elif command == zk.CMD_USER_WRQ:
                    self._write_user(data, session_id, reply_id)
                else:
                    self.send(zk.CMD_ACK_ERROR, session_id, reply_id)
        except (ConnectionError, OSError, struct.error):
            return
        finally:
            dev.untrack(sock)

    def _write_user(self, data: bytes, session_id: int, reply_id: int) -> None:
        dev = self.device
        try:
            if dev.layout == "legacy":
                _uid, _priv, _pw, name, _card, _grp, _tz, user_id = struct.unpack(zk._USER_28, data[:28])
                user_id = str(user_id)
            else:
                _uid, _priv, _pw, name, _card, _grp, raw_id = struct.unpack(zk._USER_STRUCT, data[:72])
                user_id = raw_id.split(b"\x00", 1)[0].decode("ascii", "ignore")
            full = name.split(b"\x00", 1)[0].decode("utf-8", "ignore") or user_id
        except struct.error:
            self.send(zk.CMD_ACK_ERROR, session_id, reply_id)
            return
        ok = dev.enroll(user_id, full) is not None
        self.send(zk.CMD_ACK_OK if ok else zk.CMD_ACK_ERROR, session_id, reply_id)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_server(device: Device, host: str = "127.0.0.1", port: int = 4370) -> Server:
    handler = type("BoundSession", (Session,), {"device": device})
    return Server((host, port), handler)


# --------------------------------------------------------------------------- #
# Live punches and the control API
# --------------------------------------------------------------------------- #
def simulate(device: Device, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        with device.lock:
            now = device.now_local()
            people = [u["user_id"] for u in device.users]
            random.shuffle(people)
            for user_id in people:
                recent = [r for r in device.log if r["user_id"] == user_id]
                if recent and (now - datetime.strptime(recent[-1]["time"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 30:
                    continue
                rec = device.punch(user_id)
                kind = "check-in " if rec["state"] == STATE_IN else "check-out"
                print(f"  simulated  {kind}  user {user_id:<6} {rec['time']}", flush=True)
                break


def build_control(device: Device, host: str, port: int) -> ThreadingHTTPServer:
    class Control(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *args):
            pass

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") != "/state":
                return self._json({"error": "not found"}, 404)
            with device.lock:
                inside = sorted({r["user_id"] for r in device.log if device.last_state(r["user_id"]) == STATE_IN})
                self._json({"serial": device.serial, "offline": device.offline, "users": len(device.users),
                            "log_records": len(device.log), "currently_in": inside,
                            "last_punch": device.log[-1] if device.log else None})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "body must be JSON"}, 400)
            path = self.path.rstrip("/")
            if path == "/punch":
                state = {"in": STATE_IN, "out": STATE_OUT, None: None, "": None}.get(
                    str(body["state"]).lower() if body.get("state") is not None else None, body.get("state"))
                try:
                    return self._json(device.punch(str(body.get("emp_code", "")).strip(), state))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if path == "/offline":
                device.go_offline()
                return self._json({"offline": True})
            if path == "/online":
                device.offline = False
                return self._json({"offline": False})
            if path == "/clear-log":
                with device.lock:
                    device.log.clear()
                    device.save()
                return self._json({"log_records": 0})
            self._json({"error": "not found"}, 404)

    return ThreadingHTTPServer((host, port), Control)


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company", type=int, default=None,
                        help="REQUIRED. The one Odoo company this device belongs to (use --list-companies)")
    parser.add_argument("--port", type=int, default=4370, help="The device's own port (default 4370)")
    parser.add_argument("--control-port", type=int, default=None,
                        help="HTTP control port (default: --port + 1000)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Address to bind (default loopback; use 0.0.0.0 if BioBridge is elsewhere)")
    parser.add_argument("--tz", default="Asia/Dubai",
                        help="The device's own timezone — set the connection's Device Timezone to match")
    parser.add_argument("--comm-key", type=int, default=0, help="The comm key the device demands (default 0)")
    parser.add_argument("--layout", choices=["modern", "legacy"], default="modern",
                        help="Firmware record layout (default modern; legacy = numeric ids only)")
    parser.add_argument("--no-simulate", dest="simulate", action="store_false",
                        help="Do not generate live punches")
    parser.add_argument("--interval", type=float, default=20.0, help="Seconds between simulated punches (default 20)")
    parser.add_argument("--reset-log", action="store_true", help="Start with an empty attendance log")
    from tools.mock_roster import (
        add_odoo_arguments, list_companies, load_roster, resolve_scope, tenant_token,
    )

    add_odoo_arguments(parser)
    args = parser.parse_args()
    if args.list_companies:
        raise SystemExit(list_companies(args))

    if args.company is None:
        raise SystemExit("--company is required: a standalone device belongs to exactly one company "
                         "(see --list-companies).")
    scope = resolve_scope(args)                 # also enforces --tenant and that the company is enabled
    roster = load_roster(args, args.company, directory=scope.data_dir, tenant=scope.tenant)
    serial = f"MOCK-{tenant_token(scope.tenant)}-C{args.company}-ZK-01"
    log_path = scope.data_dir / f"zk_{serial}.json"
    if args.reset_log and log_path.exists():
        log_path.unlink()
    device = Device(serial, args.tz, args.layout, args.comm_key, log_path)

    skipped = []
    for emp in roster.employees:
        name = f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip() or str(emp["emp_code"])
        if device.enroll(str(emp["emp_code"]), name) is None:
            skipped.append(str(emp["emp_code"]))

    server = build_server(device, args.host, args.port)
    control_port = args.control_port or args.port + 1000
    control = build_control(device, "127.0.0.1", control_port)
    threading.Thread(target=control.serve_forever, daemon=True).start()

    print(f"mock ZKTeco terminal {serial} on {args.host}:{args.port}  (company {args.company}: "
          f"{roster.company_name}, tenant {scope.tenant})", flush=True)
    print(f"  add a standalone-device connection with the address  zk://{args.host}:{args.port}"
          f"{'' if not args.comm_key else f'  and comm key {args.comm_key}'}", flush=True)
    print(f"  device timezone {args.tz} — set the connection's Device Timezone to match", flush=True)
    print(f"  {len(device.users)} enrolled user(s), {len(device.log)} record(s) in the log"
          f"{'; skipped non-numeric ids on legacy firmware: ' + ', '.join(skipped[:6]) if skipped else ''}",
          flush=True)
    print(f"  control: curl http://127.0.0.1:{control_port}/state   (POST /punch, /offline, /online, /clear-log)",
          flush=True)
    if args.simulate:
        threading.Thread(target=simulate, args=(device, args.interval, threading.Event()), daemon=True).start()
        print(f"  SIMULATING: one punch every {args.interval:g}s, starting now", flush=True)
    if args.host == "127.0.0.1":
        print("  loopback only — pass --host 0.0.0.0 if BioBridge is not on this machine", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
