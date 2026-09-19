#!/usr/bin/env python3
"""A fake ZKTeco BioTime server, so the whole loop works with no hardware.

Mimics the parts the connector talks to: token auth, DRF-style pagination with
an absolute ``next`` URL, and the three list endpoints. Punch data comes from a
JSON file so a test can change the stream between sync cycles.

    python3 tools/mock_biotime.py --port 8099 --punches punches.json
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

DEPARTMENTS = [
    {"id": 1, "dept_code": "1", "dept_name": "Production"},
    {"id": 2, "dept_code": "2", "dept_name": "Logistics"},
    {"id": 3, "dept_code": "3", "dept_name": "Administration"},
]

EMPLOYEES = [
    {"id": 11, "emp_code": "1001", "first_name": "Ahmed", "last_name": "Sharma",
     "department": DEPARTMENTS[0], "enable_attendance": True},
    {"id": 12, "emp_code": "0042", "first_name": "Sara", "last_name": "Tanaka",
     "department": DEPARTMENTS[1], "enable_attendance": True},
    {"id": 13, "emp_code": "A7", "first_name": "Jane", "last_name": "Haddad",
     "department": DEPARTMENTS[2], "enable_attendance": True},
]

TERMINALS = [
    {"id": 101, "sn": "MOCK-GATE-01", "alias": "Main Gate", "ip_address": "10.0.0.11"},
    {"id": 102, "sn": "MOCK-GATE-02", "alias": "Back Door", "ip_address": "10.0.0.12"},
]

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
        "--host", default="127.0.0.1",
        help="Address to bind. The default is loopback-only, which is right for "
             "a BioBridge on the same machine and invisible to one in a "
             "container or on another host — use 0.0.0.0 for those.",
    )
    args = parser.parse_args()

    global PUNCH_FILE
    PUNCH_FILE = args.punches

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
    if args.host == "127.0.0.1":
        print("  loopback only — pass --host 0.0.0.0 if BioBridge is not on this machine",
              flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
