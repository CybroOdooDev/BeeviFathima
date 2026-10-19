"""Transport failures must arrive as ProviderError, never as raw httpx.

This file exists because of a live incident: a customer's BioTime server was
off, ``httpx.ConnectError`` escaped the provider unwrapped, and because it is
not a ``ProviderError`` it sailed straight past the per-source handler in the
sync engine — the one whose entire job is to stop one dead site from killing
the others — and landed in the catch-all as "Unexpected error: [Errno 111]
Connection refused" with a stack trace.

The tests bind a real socket and close it to get a genuinely refused port,
rather than mocking ``httpx``. A mock would have passed against the broken code
just as easily, because the bug was never in *raising* the error — it was in
which exception class crossed the provider boundary.
"""

from __future__ import annotations

import socket

import httpx
import pytest
from tenacity import wait_none

from app.integrations.base import ProviderError, SourceConfig
from app.integrations.providers.biotime import (
    BioTimeProvider,
    _describe_transport_error,
)


@pytest.fixture(autouse=True)
def no_backoff():
    """Keep the retries, drop the sleeping between them.

    Three attempts with exponential backoff is right in production and pure dead
    weight here — it put 20 seconds on the suite for waits nobody is measuring.
    Swapping tenacity's ``wait`` leaves the attempt *count* intact, which is the
    part these tests care about; ``test_transport_errors_are_retried`` asserts it
    is still three.
    """
    original = BioTimeProvider._send.retry.wait  # noqa: SLF001
    BioTimeProvider._send.retry.wait = wait_none()  # noqa: SLF001
    yield
    BioTimeProvider._send.retry.wait = original  # noqa: SLF001


def _dead_port() -> int:
    """A port nothing is listening on.

    Bind, read the assigned port, close. There is a race in principle — the OS
    could hand the port to someone else before the test connects — but no other
    listener is starting inside a test run, and the alternative (a hardcoded
    port) fails on any machine that happens to use it.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _provider(base_url: str) -> BioTimeProvider:
    return BioTimeProvider(
        SourceConfig(
            base_url=base_url,
            username="u",
            password="p",
            token=None,
            options={"auth_type": "token"},
            verify_ssl=False,
        )
    )


@pytest.fixture
def refused_url() -> str:
    return f"http://127.0.0.1:{_dead_port()}"


# --------------------------------------------------------------------------- #
# The regression itself
# --------------------------------------------------------------------------- #
def test_fetch_punches_raises_provider_error_not_httpx(refused_url: str) -> None:
    """The exact shape of the incident: iterate punches against a dead server."""
    provider = _provider(refused_url)
    try:
        with pytest.raises(ProviderError):
            list(provider.fetch_punches())
    finally:
        provider.close()


def test_connect_error_does_not_escape_as_httpx(refused_url: str) -> None:
    """Belt and braces: assert the negative directly.

    ``ProviderError`` is what the engine catches; if httpx ever leaks again this
    fails on the class, not on a message.
    """
    provider = _provider(refused_url)
    try:
        with pytest.raises(ProviderError) as caught:
            list(provider.fetch_punches())
        assert not isinstance(caught.value, httpx.HTTPError)
        # The original is kept for the logs, just not raised.
        assert isinstance(caught.value.__cause__, httpx.RequestError)
    finally:
        provider.close()


def test_transport_errors_are_retried(refused_url: str, monkeypatch) -> None:
    """Wrapping must not have cost us the retry.

    The obvious fix — a try/except inside ``_send`` — turns the httpx error into
    a BioTimeError before tenacity sees it, so nothing matches the retry
    predicate and a single dropped packet fails the run. This is the test that
    says which side of the decorator the wrapping belongs on.
    """
    provider = _provider(refused_url)
    attempts = 0
    real_request = provider._client.request  # noqa: SLF001

    def counting(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return real_request(*args, **kwargs)

    monkeypatch.setattr(provider._client, "request", counting)  # noqa: SLF001
    try:
        with pytest.raises(ProviderError):
            provider._authenticate()  # noqa: SLF001
    finally:
        provider.close()
    assert attempts == 3, "a transient blip should get two more goes"


def test_authenticate_wraps_transport_errors(refused_url: str) -> None:
    provider = _provider(refused_url)
    try:
        with pytest.raises(ProviderError):
            provider._authenticate()  # noqa: SLF001
    finally:
        provider.close()


def test_test_connection_reports_instead_of_raising(refused_url: str) -> None:
    """The Connections page must show a message, not a 500."""
    provider = _provider(refused_url)
    try:
        info = provider.test_connection()
    finally:
        provider.close()
    assert info.ok is False
    assert "Cannot reach BioTime" in info.message


# --------------------------------------------------------------------------- #
# The message a customer actually reads
# --------------------------------------------------------------------------- #
def test_message_names_the_address_and_the_cause(refused_url: str) -> None:
    provider = _provider(refused_url)
    try:
        with pytest.raises(ProviderError) as caught:
            list(provider.fetch_punches())
    finally:
        provider.close()

    message = str(caught.value)
    assert refused_url in message, "the address is the first thing to check"
    assert "refused" in message.lower()
    assert "Errno" not in message, "errno numbers are not an explanation"
    assert "Traceback" not in message


def test_refused_and_unresolvable_read_differently() -> None:
    """A wrong port and a wrong hostname need different advice."""
    refused = _describe_transport_error(
        "http://box:8090", httpx.ConnectError("[Errno 111] Connection refused")
    )
    unresolved = _describe_transport_error(
        "http://box:8090",
        httpx.ConnectError("[Errno -2] Name or service not known"),
    )
    assert "listening" in refused
    assert "resolve" in unresolved
    assert refused != unresolved


def test_read_timeout_is_not_reported_as_refused() -> None:
    message = _describe_transport_error("http://box:8090", httpx.ReadTimeout("slow"))
    assert "refused" not in message.lower()
    assert "did not reply" in message


def test_connect_timeout_suggests_firewall_not_a_dead_process() -> None:
    """A dropped packet and a refused packet mean opposite things."""
    message = _describe_transport_error("http://box:8090", httpx.ConnectTimeout("t"))
    assert "firewall" in message.lower() or "unreachable" in message.lower()
    assert "refused" not in message.lower()


def test_message_fits_the_status_column(refused_url: str) -> None:
    """``source.status_message`` truncates at 500 — the advice must survive."""
    message = _describe_transport_error(refused_url, httpx.ConnectError("refused"))
    assert len(message) <= 500
