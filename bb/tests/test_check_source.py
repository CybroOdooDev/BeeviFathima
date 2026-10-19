"""The connection diagnostic must distinguish causes, not just report failure.

Every layer it checks has a different fix, and getting the layer wrong sends
someone to restart a service when the real problem is a firewall. These tests
bind real sockets rather than mocking, for the same reason the provider tests
do: the value of this tool is entirely in what a real socket does.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tools.check_source import _summarise, _wrap, check


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Handler(BaseHTTPRequestHandler):
    """Enough BioTime to exercise the auth and data layers."""

    accept_credentials = True

    def log_message(self, *_args):  # noqa: D102 — silence the test output
        pass

    def _send(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802
        if self.path == "/api-token-auth/":
            if type(self).accept_credentials:
                return self._send(b'{"token": "t"}')
            return self._send(b'{"detail": "bad"}', 401)
        self._send(b'{"detail": "Not found."}', 404)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/iclock/api/transactions/"):
            return self._send(b'{"count": 7, "next": null, "data": []}')
        self._send(b"{}")


@pytest.fixture
def biotime():
    """A stand-in BioTime on a real port, torn down after the test."""
    _Handler.accept_credentials = True
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _Handler
    server.shutdown()
    server.server_close()


# --------------------------------------------------------------------------- #
# Each layer is named correctly
# --------------------------------------------------------------------------- #
def test_healthy_connection_passes_every_layer(biotime) -> None:
    url, _ = biotime
    report = check(url, "admin", "pw")
    assert report.failed_at is None, report.lines
    assert any("7 punch" in message for _, message in report.lines)


def test_refused_port_stops_at_tcp() -> None:
    report = check(f"http://127.0.0.1:{_free_port()}", "admin", "pw")
    assert report.failed_at == "tcp"
    assert any("refused" in item.lower() for item in report.advice)


def test_refusal_suggests_the_port_that_is_actually_open(biotime) -> None:
    """The commonest real cause: the service moved, or was started elsewhere.

    This is the mock-on-8099 / configured-for-8090 mismatch, which produces a
    failure that never resolves on its own and reads identically to a dead
    server.
    """
    url, _ = biotime
    live_port = int(url.rsplit(":", 1)[1])

    # Ask about a neighbouring port that nothing is on, and have the scan
    # discover the live one.
    from tools import check_source

    monkey = list(check_source.NEARBY_PORTS)
    check_source.NEARBY_PORTS = [live_port]
    try:
        report = check(f"http://127.0.0.1:{_free_port()}", "admin", "pw")
    finally:
        check_source.NEARBY_PORTS = monkey

    assert report.failed_at == "tcp"
    assert any(str(live_port) in item for item in report.advice), (
        "the open port is the whole answer — it must appear in the advice"
    )


def test_unresolvable_host_stops_at_dns() -> None:
    report = check("http://biotime.invalid.test:8090", "admin", "pw")
    assert report.failed_at == "dns"
    assert any("IP address" in item for item in report.advice)


def test_https_against_a_plain_http_port_stops_at_tls(biotime) -> None:
    url, _ = biotime
    report = check(url.replace("http://", "https://"), "admin", "pw")
    assert report.failed_at == "tls"
    assert any("http://" in item for item in report.advice)


def test_bad_credentials_are_not_reported_as_unreachable(biotime) -> None:
    """The opposite mistake: blaming the network for a typo in a password."""
    url, handler = biotime
    handler.accept_credentials = False
    report = check(url, "admin", "wrong")
    assert report.failed_at == "auth"
    assert any("password" in item for item in report.advice)
    assert not any("refused" in item.lower() for item in report.advice)


def test_missing_scheme_is_caught_before_any_network_call() -> None:
    report = check("10.0.0.9:8090", "admin", "pw")
    assert report.failed_at == "url"
    assert len(report.lines) == 1, "no point resolving anything yet"


# --------------------------------------------------------------------------- #
# A timeout is not a refusal
# --------------------------------------------------------------------------- #
def test_timeout_does_not_suggest_a_wrong_port(monkeypatch) -> None:
    """A filter drops every port, so a scan can only mislead here.

    Worth a test because scanning on any TCP failure is the obvious
    implementation, and it produces confident advice pointing at the wrong
    cause — the one kind of wrong answer a diagnostic must not give.
    """
    from tools import check_source

    monkeypatch.setattr(
        check_source, "port_open",
        lambda host, port, timeout=3.0: (False, "TimeoutError: timed out", 3.0),
    )

    def explode(*_args, **_kwargs):  # pragma: no cover — asserts it is not called
        raise AssertionError("a timed-out host must not be port-scanned")

    monkeypatch.setattr(check_source, "scan_nearby", explode)

    report = check_source.check("http://10.255.255.1:8090", "admin", "pw")
    assert report.failed_at == "tcp"
    assert any("firewall" in item.lower() for item in report.advice)
    assert not any("moved" in item for item in report.advice)


# --------------------------------------------------------------------------- #
# An open port that will not talk HTTP has three causes, not one
# --------------------------------------------------------------------------- #
class _Slow(BaseHTTPRequestHandler):
    delay = 0.6

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        import time

        time.sleep(type(self).delay)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def test_a_slow_server_is_not_called_a_non_web_service(monkeypatch) -> None:
    """The one that cost real time: "not a web server" when it plainly is.

    A read timeout says nothing about what is on the port — only that it did not
    answer in the window allowed. Blaming the wrong thing sends someone hunting
    for a rogue service instead of raising a timeout.
    """
    from http.server import ThreadingHTTPServer

    from tools import check_source

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Impatient client, patient probe: exactly the production shape.
    monkeypatch.setattr(check_source, "HTTP_TIMEOUT", 0.2)
    try:
        report = check_source.check(
            f"http://127.0.0.1:{server.server_port}", "admin", "pw"
        )
    finally:
        server.shutdown()
        server.server_close()

    assert report.failed_at == "http"
    assert any("IS a web server" in item for item in report.advice)
    assert not any("not a web server" in item for item in report.advice)
    assert any("TIMEOUT" in item for item in report.advice), "name the knob to turn"


def test_a_wedged_single_threaded_server_is_named_as_such() -> None:
    """Accepts connections, serves none — the failure that motivated all this.

    A single-threaded server blocked on one abandoned connection keeps accepting
    into the kernel backlog, so a TCP check passes instantly while nothing is
    ever answered. It looks identical to a healthy port from the outside.
    """
    from tools import check_source

    server = HTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    hold = socket.create_connection(("127.0.0.1", port))  # never sends a request
    check_source.HTTP_TIMEOUT, check_source.PROBE_TIMEOUT = 0.3, 0.6
    try:
        assert check_source.port_open("127.0.0.1", port)[0], (
            "the port must still look open — that is the trap"
        )
        report = check_source.check(f"http://127.0.0.1:{port}", "admin", "pw")
    finally:
        check_source.HTTP_TIMEOUT, check_source.PROBE_TIMEOUT = 10.0, 20.0
        hold.close()
        server.shutdown()
        server.server_close()

    assert report.failed_at == "http"
    assert any("sent nothing" in message for _, message in report.lines)
    assert any("single-threaded" in item for item in report.advice)


def test_a_non_http_service_shows_its_banner() -> None:
    """The banner is the answer — it names what actually owns the port."""
    import socketserver

    class _Banner(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b"SSH-2.0-OpenSSH_8.9p1\r\n")

    from tools import check_source

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Banner)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        report = check_source.check(f"http://127.0.0.1:{port}", "admin", "pw")
    finally:
        server.shutdown()
        server.server_close()

    assert report.failed_at == "http"
    assert any("SSH-2.0" in message for _, message in report.lines), (
        "print what answered; naming the service is the whole finding"
    )


# --------------------------------------------------------------------------- #
# Output stays readable
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


def test_html_error_pages_are_reduced_to_a_phrase() -> None:
    body = "<html><head><title>502 Bad Gateway</title></head><body>" + "x" * 5000
    assert _summarise(_FakeResponse(body)) == "502 Bad Gateway"


def test_long_bodies_are_truncated() -> None:
    assert len(_summarise(_FakeResponse("y" * 500))) <= 81


def test_empty_body_says_so() -> None:
    assert _summarise(_FakeResponse("")) == "empty body"


def test_wrap_never_loses_a_word() -> None:
    text = "The connection was refused, which means the host is up and answered."
    assert " ".join(_wrap(text, 20)).split() == text.split()


# --------------------------------------------------------------------------- #
# The mock must not be the thing that breaks
# --------------------------------------------------------------------------- #
def test_mock_survives_abandoned_connections() -> None:
    """A stray socket must not take the mock down.

    It used to run on a single-threaded ``HTTPServer``, so one client that
    connected and never finished a request blocked every subsequent one —
    forever. The kernel kept accepting into the backlog, so the port stayed
    "open" while nothing was served, which is indistinguishable from a hung
    BioTime and sends you debugging the wrong machine. A browser tab left open
    on the mock is enough to trigger it.
    """
    import httpx

    from tools.mock_biotime import build_server

    # build_server, not a hand-rolled ThreadingHTTPServer: the point is to
    # exercise the construction the tool actually uses, so this fails if it
    # goes back to the single-threaded one.
    server = build_server()
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    abandoned = [socket.create_connection(("127.0.0.1", port)) for _ in range(5)]
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/api-token-auth/",
            json={"username": "a", "password": "b"},
            timeout=5.0,
        )
        assert response.status_code == 200, "five dead sockets must not wedge it"
        assert response.json()["token"]
    finally:
        for sock in abandoned:
            sock.close()
        server.shutdown()
        server.server_close()
