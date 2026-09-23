"""BioTimeProvider.create_employee() / fetch_employees(), against a small
self-contained fake server — not tools/mock_biotime.py, deliberately: that
tool's EMPLOYEES list is module-level state shared by every test process
that imports it, and a real create-employee endpoint needs to mutate a
roster per test without one test's POST leaking into the next. A fake
scoped to this file keeps that isolation without touching a tool other
tests (and manual runs) also rely on.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.integrations.base import Capability, EmployeeRecord, ProviderError, SourceConfig
from app.integrations.providers.biotime import BioTimeProvider


class _Handler(BaseHTTPRequestHandler):
    """Enough BioTime to exercise personnel create + list."""

    #: Reset per test via the fixture below — never mutated at class-body
    #: scope, so no state survives between tests.
    employees: list[dict] = []
    token = "tok"

    def log_message(self, *_args):  # noqa: D102 — silence test output
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if header != f"Token {type(self).token}":
            self._send({"detail": "Invalid token."}, 401)
            return False
        return True

    def do_POST(self):  # noqa: N802
        if self.path == "/api-token-auth/":
            return self._send({"token": type(self).token})
        if self.path == "/personnel/api/employees/":
            if not self._authorised():
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            emp_code = body.get("emp_code")
            existing = next(
                (e for e in type(self).employees if e["emp_code"] == emp_code), None
            )
            if existing is not None:
                return self._send(
                    {"emp_code": ["employee with this emp_code already exists."]}, 400
                )
            new_id = max([e["id"] for e in type(self).employees], default=0) + 1
            row = {
                "id": new_id,
                "emp_code": emp_code,
                "first_name": body.get("first_name", ""),
                "last_name": body.get("last_name", ""),
                "enable_attendance": body.get("enable_attendance", True),
                "department": None,
            }
            type(self).employees.append(row)
            return self._send(row, 201)
        self._send({"detail": "Not found."}, 404)

    def do_GET(self):  # noqa: N802
        if not self._authorised():
            return
        if self.path.startswith("/personnel/api/employees/"):
            return self._send(
                {"count": len(type(self).employees), "next": None, "previous": None,
                 "data": list(type(self).employees)}
            )
        self._send({}, 200)


@pytest.fixture
def biotime():
    _Handler.employees = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _provider(base_url: str) -> BioTimeProvider:
    return BioTimeProvider(
        SourceConfig(base_url=base_url, username="u", password="p", verify_ssl=False)
    )


def test_write_employees_capability_is_declared():
    assert Capability.WRITE_EMPLOYEES in BioTimeProvider.capabilities
    assert Capability.READ_EMPLOYEES in BioTimeProvider.capabilities


def test_create_employee_posts_and_returns_the_created_record(biotime):
    provider = _provider(biotime)
    try:
        created = provider.create_employee(
            EmployeeRecord(external_id=None, emp_code="9001", first_name="New", last_name="Hire")
        )
    finally:
        provider.close()

    assert created.emp_code == "9001"
    assert created.first_name == "New"
    assert created.last_name == "Hire"
    assert created.external_id is not None
    assert created.is_active is True


def test_created_employee_is_then_visible_via_fetch_employees(biotime):
    provider = _provider(biotime)
    try:
        provider.create_employee(EmployeeRecord(external_id=None, emp_code="9002", first_name="A"))
        codes = {e.emp_code for e in provider.fetch_employees()}
    finally:
        provider.close()
    assert "9002" in codes


def test_create_employee_rejects_a_blank_emp_code(biotime):
    provider = _provider(biotime)
    try:
        with pytest.raises(ProviderError):
            provider.create_employee(EmployeeRecord(external_id=None, emp_code="   "))
    finally:
        provider.close()


def test_create_employee_is_idempotent_for_a_duplicate_code(biotime):
    """Someone else — a previous partially-failed run, a manual entry in
    BioTime — already created this emp_code. BioTime's 400 for a duplicate
    must resolve to the existing record, not a hard failure."""
    provider = _provider(biotime)
    try:
        first = provider.create_employee(
            EmployeeRecord(external_id=None, emp_code="9003", first_name="Original")
        )
        second = provider.create_employee(
            EmployeeRecord(external_id=None, emp_code="9003", first_name="Retry-Attempt")
        )
    finally:
        provider.close()

    assert second.external_id == first.external_id
    assert second.first_name == "Original", "the existing record wins, not the retry's data"
