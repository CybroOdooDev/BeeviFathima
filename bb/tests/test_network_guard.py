"""The suite runs offline: outside hosts are refused at once, the machine's
own servers still work, and the app's database is not the project's file."""
import os
import socket

import pytest

from tests.conftest import NetworkBlocked


def test_outside_hosts_are_refused_immediately():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("acme.odoo.com", 443)
    with socket.socket() as s, pytest.raises(NetworkBlocked):
        s.connect(("10.255.255.1", 80))


def test_loopback_still_works():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with server, socket.create_connection(server.getsockname(), timeout=2):
        pass


def test_app_database_is_a_temporary_file():
    from app.core.config import settings
    assert "bb-tests-" in settings.database_url
    assert not settings.database_url.endswith("./biobridge.db")
    assert os.environ.get("HTTPS_PROXY") is None
