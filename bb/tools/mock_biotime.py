#!/usr/bin/env python3
"""A fake ZKTeco BioTime server, so the whole loop works with no hardware.

Mimics the parts the connector talks to: token auth, DRF-style pagination with
an absolute ``next`` URL, and the three list endpoints. Punch data comes from a
JSON file so a test can change the stream between sync cycles.

    python3 tools/mock_biotime.py --port 8099 --punches punches.json

Serves one company's roster at a time, chosen with ``--company``. Company 1
(the default, unchanged from before ``--company`` existed) is Ahmed Sharma /
Sara Tanaka / Jane Haddad on MOCK-GATE-01/02. Company 2 is Liam Okafor / Priya
Nakamura / Noah Fernandes on MOCK-GATE-03/04 — the same three people
tools/create_company_employees.py creates in a real Odoo, so the two tools
describe the same fake "company 2" everywhere. Company 4 is Beevi (badge 5) / Marc
(badge 6001) on MOCK-GATE-05/06. Run multiple instances on different
ports, one per --company, to drive BioBridge sources against multiple OdooConnections
that are scoped to different company_id values and confirm each only ever sees
its own roster:

    python3 tools/mock_biotime.py --port 8099 --company 1 &
    python3 tools/mock_biotime.py --port 8098 --company 2 &
    python3 tools/mock_biotime.py --port 8097 --company 4 &
"""

from __future__ import annotations

import argparse
import json
import os
import socket
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
            _emp(42, "6001", "marc", "", _COMPANY_4_DEPTS[1]),
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

PUNCH_FILE: str | None = None


def _punches() -> list[dict]:
    if PUNCH_FILE and os.path.exists(PUNCH_FILE):
        with open(PUNCH_FILE) as handle:
            return json.load(handle)
    return []


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
        self._json({"detail": "Not found."}, 404)

    def do_GET(self):
        if not self._authorised():
            return

        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/personnel/api/departments/":
            return self._json(self._page(DEPARTMENTS, query, path))
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
    parser.add_argument("--punches", default=None)
    parser.add_argument(
        "--company", type=int, default=1, choices=sorted(DATASETS),
        help="Which fake roster to serve: 1 (default) is Ahmed Sharma / Sara "
             "Tanaka / Jane Haddad on MOCK-GATE-01/02; 2 is Liam Okafor / "
             "Priya Nakamura / Noah Fernandes on MOCK-GATE-03/04 — the same "
             "people tools/create_company_employees.py makes in a real Odoo; "
             "4 is Beevi (5) / Marc (6001) on MOCK-GATE-05/06. "
             "Run one instance per company, on different ports, to test "
             "BioBridge sources against multiple company_id-scoped OdooConnections.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Address to bind. The default is loopback-only, which is right for "
             "a BioBridge on the same machine and invisible to one in a "
             "container or on another host — use 0.0.0.0 for those.",
    )
    args = parser.parse_args()

    global PUNCH_FILE, DEPARTMENTS, EMPLOYEES, TERMINALS
    PUNCH_FILE = args.punches
    dataset = DATASETS[args.company]
    DEPARTMENTS = dataset["departments"]
    EMPLOYEES = dataset["employees"]
    TERMINALS = dataset["terminals"]

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
    names = ", ".join(f"{e['first_name']} {e['last_name']}".strip() for e in EMPLOYEES)
    print(f"  company {args.company}: {names}", flush=True)
    if args.host == "127.0.0.1":
        print("  loopback only — pass --host 0.0.0.0 if BioBridge is not on this machine",
              flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
