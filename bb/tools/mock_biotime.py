#!/usr/bin/env python3
"""A fake ZKTeco BioTime server, so the whole loop works with no hardware.

Mimics the parts the connector talks to: token auth, DRF-style pagination with
an absolute ``next`` URL, and the three list endpoints. Punch data comes from a
JSON file (tools/generate_punches.py) so the stream can change between syncs.

Live punches — a simulator and a manual trigger, so a sync always has something
fresh to fetch (stale files and reused ids are what make a sync "see nothing"):

    python3 tools/mock_biotime.py --company 4 --port 8007 --tz Asia/Kolkata --simulate
    curl -X POST localhost:8007/mock/punch -d '{"emp_code": "5"}'      # toggles in/out
    curl -X POST localhost:8007/mock/punch -d '{"emp_code": "5", "state": "out"}'
    curl localhost:8007/mock/state                                      # who is in

Serve any company in your Odoo by its res.company id — its active employees
become the BioTime personnel list, on two mock terminals of its own:

    python3 tools/generate_punches.py --company 7
    python3 tools/mock_biotime.py --company 7 --port 8107

The punches file defaults to the one generate_punches.py writes for that
company (punches.json for company 1, punches_company<id>.json otherwise). Run
one instance per company on different ports to test several BioBridge
accounts at once. Where the roster comes from (Odoo login, cache, built-in
fixtures for 1/2/4) is described in tools/mock_roster.py.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "mock-token"
PAGE_CAP = 2  # tiny on purpose, so pagination is exercised by every run


def _dept(id_: int, code: str, name: str) -> dict:
    return {"id": id_, "dept_code": code, "dept_name": name}


def _emp(id_: int, code: str, first: str, last: str, dept: dict) -> dict:
    return {"id": id_, "emp_code": code, "first_name": first, "last_name": last,
             "department": dept, "enable_attendance": True}


def _term(id_: int, sn: str, alias: str, ip: str) -> dict:
    return {"id": id_, "sn": sn, "alias": alias, "ip_address": ip}


def _dataset(departments: list[dict], employees: list[dict], terminals: list[dict]) -> dict:
    return {"departments": departments, "employees": employees, "terminals": terminals}


_COMPANY_1_DEPTS = [
    _dept(1, "1", "Production"),
    _dept(2, "2", "Logistics"),
    _dept(3, "3", "Administration"),
]

_COMPANY_2_DEPTS = [
    _dept(1, "1", "Engineering"),
    _dept(2, "2", "Sales"),
    _dept(3, "3", "Facilities"),
]

_COMPANY_4_DEPTS = [
    _dept(1, "1", "Operations"),
    _dept(2, "2", "Quality"),
    _dept(3, "3", "Support"),
]

#: Three self-contained rosters, selected at startup by --company. Every id
#: (department, employee, terminal) restarts from the same small numbers in
#: each dataset — the rosters are never mixed into one process, so nothing needs
#: them to be globally unique, and it keeps each dataset readable on its own.
DATASETS: dict[int, dict] = {
    1: _dataset(
        _COMPANY_1_DEPTS,
        [
            _emp(11, "1001", "Ahmed", "Sharma", _COMPANY_1_DEPTS[0]),
            _emp(12, "0042", "Sara", "Tanaka", _COMPANY_1_DEPTS[1]),
            _emp(13, "A7", "Jane", "Haddad", _COMPANY_1_DEPTS[2]),
        ],
        [
            _term(101, "MOCK-GATE-01", "Main Gate", "10.0.0.11"),
            _term(102, "MOCK-GATE-02", "Back Door", "10.0.0.12"),
        ],
    ),
    #: Same three people tools/create_company_employees.py's SAMPLE_EMPLOYEES
    #: creates in a real Odoo (company-2 flavored, deliberately distinct names
    #: and codes from company 1's roster above so the two are never confused).
    2: _dataset(
        _COMPANY_2_DEPTS,
        [
            _emp(21, "2001", "Liam", "Okafor", _COMPANY_2_DEPTS[0]),
            _emp(22, "2002", "Priya", "Nakamura", _COMPANY_2_DEPTS[1]),
            _emp(23, "2003", "Noah", "Fernandes", _COMPANY_2_DEPTS[2]),
        ],
        [
            _term(201, "MOCK-GATE-03", "North Entrance", "10.0.1.11"),
            _term(202, "MOCK-GATE-04", "Loading Bay", "10.0.1.12"),
        ],
    ),
    #: Company 4: Beevi (badge 5) and Marc (badge 6001). No last names —
    #: BioTime sends last_name as an empty string when none is enrolled.
    4: _dataset(
        _COMPANY_4_DEPTS,
        [
            _emp(41, "5", "Beevi", "", _COMPANY_4_DEPTS[0]),
            _emp(42, "6001", "Marc", "", _COMPANY_4_DEPTS[1]),
        ],
        [
            _term(401, "MOCK-GATE-05", "East Entrance", "10.0.2.11"),
            _term(402, "MOCK-GATE-06", "West Exit", "10.0.2.12"),
        ],
    ),
}

# Populated from DATASETS[args.company] in main() — default to company 1 so
# every existing caller (tests, other tools) that never passes --company sees
# exactly the roster this file always had.
DEPARTMENTS = DATASETS[1]["departments"]
EMPLOYEES = DATASETS[1]["employees"]
TERMINALS = DATASETS[1]["terminals"]
AREAS = [{"id": 1, "area_code": "1", "area_name": "Not Authorized"}, {"id": 2, "area_code": "2", "area_name": "Head Office"}]

PUNCH_FILE: str | None = None


def _punches() -> list[dict]:
    if PUNCH_FILE and os.path.exists(PUNCH_FILE):
        with open(PUNCH_FILE) as handle:
            return json.load(handle)
    return []


# --------------------------------------------------------------------------- #
# Live punches: the simulator and the /mock/punch endpoint
# --------------------------------------------------------------------------- #
TIME_FMT = "%Y-%m-%d %H:%M:%S"
SERVER_TZ = "Asia/Dubai"       # must match the BioBridge connection's Server Timezone
_WRITE_LOCK = threading.Lock()


def _now_local() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(SERVER_TZ)).replace(tzinfo=None)


def _write_punches(rows: list[dict]) -> None:
    tmp = f"{PUNCH_FILE}.tmp"
    with open(tmp, "w") as handle:
        json.dump(rows, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, PUNCH_FILE)     # readers never see a half-written file


def _last_state(rows: list[dict], emp_code: str) -> tuple[str | None, str | None]:
    """(state, punch_time) of the employee's most recent punch, if any."""
    mine = [r for r in rows if str(r.get("emp_code")) == str(emp_code)]
    if not mine:
        return None, None
    last = max(mine, key=lambda r: (r["punch_time"], r["id"]))
    return str(last["punch_state"]), last["punch_time"]


def add_punch(emp_code: str, state: str | None = None, terminal: str | None = None,
              at: str | None = None) -> dict:
    """Append one punch to the punches file and return it.

    The id always continues from the file's highest id, and never starts below
    the current epoch second when the file is empty — so deleting or
    regenerating the file cannot hand BioBridge an id it has already stored
    (it would silently skip the punch as a duplicate). With no ``state`` the
    punch toggles: check-in first, then check-out, then check-in again.
    """
    code = str(emp_code).strip()
    if not code:
        raise ValueError("emp_code is required")
    if not PUNCH_FILE:
        raise ValueError("this mock has no punches file")
    with _WRITE_LOCK:
        rows = _punches()
        last_state, _ = _last_state(rows, code)
        if state in (None, ""):
            state = "1" if last_state == "0" else "0"
        state = {"in": "0", "out": "1"}.get(str(state).lower(), str(state))
        if state not in ("0", "1"):
            raise ValueError("state must be 0/in (check in) or 1/out (check out)")
        sns = [t["sn"] for t in TERMINALS] or ["MOCK-GATE-01"]
        row = {
            "id": max([int(r["id"]) for r in rows] + [int(time.time()) - 1]) + 1,
            "emp_code": code,
            "punch_time": at or _now_local().strftime(TIME_FMT),
            "punch_state": state,
            "verify_type": "15",
            "terminal_sn": terminal or random.choice(sns),
            "first_name": "",
            "last_name": "",
        }
        rows.append(row)
        _write_punches(rows)
    return row


def simulate(interval: float, stop: threading.Event, burst: int = 1) -> None:
    """Every ``interval`` seconds, a random employee clocks in or out *now*.

    Each person alternates in / out from wherever their last punch left them,
    and never punches twice inside 30 seconds, so the stream is a plausible
    attendance record that Odoo accepts: no overlaps, no check-out before a
    check-in, nothing in the future.
    """
    while not stop.wait(interval):
        for _ in range(burst):
            people = [str(e["emp_code"]) for e in EMPLOYEES if e.get("enable_attendance", True)]
            random.shuffle(people)
            rows = _punches()
            now = _now_local()
            for code in people:
                _, when = _last_state(rows, code)
                if when and (now - datetime.strptime(when, TIME_FMT)).total_seconds() < 30:
                    continue
                row = add_punch(code)
                kind = "check-in " if row["punch_state"] == "0" else "check-out"
                print(f"  simulated  {kind}  badge {row['emp_code']:<6} {row['punch_time']}  "
                      f"{row['terminal_sn']}  (id {row['id']})", flush=True)
                break


class Handler(BaseHTTPRequestHandler):
    # A connection that goes quiet is dropped rather than held forever. Without
    # this, a client that opens a socket and never finishes a request — a
    # browser's speculative pre-connect, a port scanner, a health check — parks
    # a thread on a blocking readline() that never returns. On the
    # single-threaded server this file used to use, one of those wedged the
    # whole mock: the port kept accepting connections (so a TCP check passed
    # instantly) while nothing was ever answered, which reads exactly like a
    # hung BioTime and cost an afternoon to find.
    timeout = 10

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (TimeoutError, socket.timeout):
            self.close_connection = True

    def log_message(self, *args):
        pass  # keep test output readable

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if header not in (f"Token {TOKEN}", f"JWT {TOKEN}"):
            self._json({"detail": "Invalid token."}, 401)
            return False
        return True

    def _page(self, rows, query, path):
        try:
            page = int(query.get("page", ["1"])[0])
        except ValueError:
            page = 1
        size = min(int(query.get("page_size", [PAGE_CAP])[0]), PAGE_CAP)

        start = (page - 1) * size
        window = rows[start:start + size]

        next_url = None
        if start + size < len(rows):
            keep = {k: v for k, v in query.items() if k != "page"}
            parts = [f"page={page + 1}"]
            parts += [f"{k}={v}" for k, values in keep.items() for v in values]
            host = self.headers.get("Host", "localhost")
            next_url = f"http://{host}{path}?{'&'.join(parts)}"

        return {"count": len(rows), "next": next_url, "previous": None, "data": window}

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/api-token-auth/", "/jwt-api-token-auth/"):
            return self._json({"token": TOKEN})
        if path == "/personnel/api/employees/":
            return self._create_employee()
        if path == "/mock/punch":
            return self._mock_punch()
        self._json({"detail": "Not found."}, 404)

    def _mock_punch(self):
        """POST /mock/punch {"emp_code": "5", "state": "in"|"out"|"0"|"1"?,
        "terminal": "SN"?, "at": "YYYY-MM-DD HH:MM:SS"?} — one punch, right now
        unless ``at`` says otherwise. No token: this is a test control, not
        part of BioTime's API."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            row = add_punch(body.get("emp_code", ""), body.get("state"),
                            body.get("terminal"), body.get("at"))
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json({"detail": str(exc)}, 400)
        self._json(row, 201)

    def _create_employee(self):
        """POST /personnel/api/employees/ — what BioBridge's "create the
        Odoo employees BioTime is missing" step calls. Held in memory only, so
        a restart of this mock forgets them (and BioBridge simply creates
        them again on the next test)."""
        if not self._authorised():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"detail": "Invalid JSON."}, 400)
        code = str(body.get("emp_code") or "").strip()
        if not code:
            return self._json({"emp_code": ["This field is required."]}, 400)
        if any(str(e.get("emp_code")) == code for e in EMPLOYEES):
            # BioTime's own wording for a duplicate; BioBridge keys on it.
            return self._json({"emp_code": ["employee with this emp code already exists."]}, 400)
        # Real BioTime rejects a personnel row with no department or area.
        missing = {f: ["This field is required."] for f in ("department", "area") if not body.get(f)}
        if missing:
            return self._json(missing, 400)
        dept = next((d for d in DEPARTMENTS if d.get("id") == body.get("department")), None)
        if dept is None:
            return self._json({"department": ["Invalid pk - object does not exist."]}, 400)
        row = _emp(
            max([int(e.get("id") or 0) for e in EMPLOYEES] + [0]) + 1,
            code,
            str(body.get("first_name") or code),
            str(body.get("last_name") or ""),
            dept,
        )
        row["enable_attendance"] = bool(body.get("enable_attendance", True))
        EMPLOYEES.append(row)
        self._json(row, 201)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/mock/state":
            rows = _punches()
            people = {}
            for e in EMPLOYEES:
                state, when = _last_state(rows, e["emp_code"])
                people[str(e["emp_code"])] = {
                    "name": f"{e.get('first_name', '')} {e.get('last_name', '')}".strip(),
                    "last_punch": when,
                    "now": {None: "no punches", "0": "checked in", "1": "checked out"}.get(state, state),
                }
            return self._json({"punches": len(rows), "server_time": _now_local().strftime(TIME_FMT),
                               "timezone": SERVER_TZ, "people": people})
        if not self._authorised():
            return

        if path == "/personnel/api/departments/":
            return self._json(self._page(DEPARTMENTS, query, path))
        if path == "/personnel/api/areas/":
            return self._json(self._page(AREAS, query, path))
        if path == "/personnel/api/employees/":
            return self._json(self._page(EMPLOYEES, query, path))
        if path == "/iclock/api/terminals/":
            return self._json(self._page(TERMINALS, query, path))

        if path == "/iclock/api/transactions/":
            rows = _punches()
            emp_code = query.get("emp_code", [None])[0]
            if emp_code:
                rows = [r for r in rows if str(r.get("emp_code")) == str(emp_code)]
            # Filter on the server's own local clock, exactly as BioTime does.
            start = query.get("start_time", [None])[0]
            end = query.get("end_time", [None])[0]
            if start:
                rows = [r for r in rows if r["punch_time"] >= start]
            if end:
                rows = [r for r in rows if r["punch_time"] <= end]
            rows = sorted(rows, key=lambda r: (r["punch_time"], r["id"]))
            return self._json(self._page(rows, query, path))

        self._json({"detail": "Not found."}, 404)


def build_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """The one place the server is constructed, so a test can exercise it.

    Split out of ``main`` deliberately: a test that builds its own
    ``ThreadingHTTPServer`` would keep passing if this file went back to the
    single-threaded one, which is exactly the regression worth guarding.
    """
    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--punches", default=None,
                        help="Punches file (default: the one generate_punches.py writes for --company)")
    parser.add_argument(
        "--company", type=int, default=1,
        help="Odoo res.company id whose active employees to serve (default: 1). "
             "Use --list-companies to see the ids.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Address to bind. The default is loopback-only, which is right for "
             "a BioBridge on the same machine and invisible to one in a "
             "container or on another host — use 0.0.0.0 for those.",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Live mode: every --interval seconds a random employee clocks in or "
             "out right now, alternating in/out, appended to the punches file.",
    )
    parser.add_argument("--reset-punches", action="store_true",
                        help="Empty the punches file before starting (ids restart from the "
                             "clock, so BioBridge will not mistake them for old ones)")
    parser.add_argument("--interval", type=float, default=20.0,
                        help="Seconds between simulated punches (default: 20)")
    parser.add_argument("--burst", type=int, default=1,
                        help="Punches per tick (default: 1)")
    parser.add_argument(
        "--tz", default="Asia/Dubai",
        help="Timezone of the punch times this server hands out — must match the "
             "BioBridge connection's Server Timezone (default: Asia/Dubai)",
    )
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.mock_roster import add_odoo_arguments, describe, list_companies, load_roster

    add_odoo_arguments(parser)
    args = parser.parse_args()
    if args.list_companies:
        return list_companies(args)

    global PUNCH_FILE, DEPARTMENTS, EMPLOYEES, TERMINALS, SERVER_TZ
    SERVER_TZ = args.tz
    roster = load_roster(args, args.company)
    DEPARTMENTS = roster.departments
    EMPLOYEES = roster.employees
    TERMINALS = roster.terminals
    PUNCH_FILE = args.punches or ("punches.json" if args.company == 1 else f"punches_company{args.company}.json")
    if args.reset_punches:
        _write_punches([])
        print(f"  emptied {PUNCH_FILE}", flush=True)
    elif os.path.exists(PUNCH_FILE):
        known = {str(e["emp_code"]) for e in EMPLOYEES}
        strangers = sorted({str(r["emp_code"]) for r in _punches()} - known)
        if strangers:
            print(f"  WARNING: {PUNCH_FILE} holds punches for badges that are not in this roster "
                  f"({', '.join(strangers[:6])}{' …' if len(strangers) > 6 else ''}) — probably left over "
                  f"from another company or Odoo. They are still served. Start with --reset-punches, or "
                  f"point --punches at a new file.", flush=True)
    if not os.path.exists(PUNCH_FILE):
        print(f"  no punches yet in {PUNCH_FILE} — run: python3 tools/generate_punches.py "
              f"--company {args.company}", flush=True)

    # Threaded: one slow or abandoned client must not stop every other
    # request. BioBridge paginates, so it holds several sequential
    # connections per sync and a wedge here stalls the whole run.
    server = build_server(args.host, args.port)
    # The port is printed because the default (8099) is not the one people
    # usually put in the connection form, and a silent mismatch looks exactly
    # like a dead server: the sync says "connection refused" forever.
    print(f"mock BioTime on http://{args.host}:{args.port} (token {TOKEN})", flush=True)
    print(
        f"  point the source's Server URL at exactly http://{args.host}:{args.port}",
        flush=True,
    )
    print(f"  {describe(roster)}", flush=True)
    print(f"  punches from {PUNCH_FILE}", flush=True)
    print(f"  punch times are in {SERVER_TZ} — set the connection's Server Timezone to match",
          flush=True)
    print("  add a punch by hand:  curl -X POST http://%s:%d/mock/punch -d '{\"emp_code\": \"5\"}'"
          % (args.host, args.port), flush=True)
    print("  who is in / out:      curl http://%s:%d/mock/state" % (args.host, args.port), flush=True)
    if args.simulate:
        if not PUNCH_FILE:
            raise SystemExit("--simulate needs a punches file")
        if not os.path.exists(PUNCH_FILE):
            _write_punches([])
        threading.Thread(target=simulate, args=(args.interval, threading.Event(), args.burst),
                         daemon=True).start()
        print(f"  SIMULATING: one punch every {args.interval:g}s, starting now", flush=True)
    if args.host == "127.0.0.1":
        print("  loopback only — pass --host 0.0.0.0 if BioBridge is not on this machine",
              flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
