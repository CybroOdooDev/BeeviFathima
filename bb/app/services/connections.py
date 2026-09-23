"""Turning stored connection rows into live clients.

This is the only module in the system that calls ``decrypt``. Credentials exist
in plaintext inside a client object and nowhere else — not in a schema, not in a
log line, not on the dashboard.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.config import settings
from app.core.crypto import decrypt
from app.integrations import providers as _providers  # noqa: F401 — registers the catalogue
from app.integrations.base import AttendanceProvider, SourceConfig, build_provider
from app.integrations.odoo import OdooClient, OdooCredentials
from app.models import DeviceSource, OdooConnection, Tenant


class UnsafeTargetError(ValueError):
    """The customer-supplied URL points somewhere we refuse to call."""


def assert_safe_url(url: str) -> None:
    """SSRF guard for customer-supplied endpoints.

    Customers legitimately run BioTime on a LAN, so this is a policy switch
    rather than a hard block: cloud deployments set
    ``allow_private_network_targets=false`` and require a public hostname or a
    tunnel, while a self-hosted install leaves it on.
    """
    if settings.allow_private_network_targets:
        return

    host = urlparse(url).hostname
    if not host:
        raise UnsafeTargetError("That URL has no host")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeTargetError(f"Cannot resolve {host}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise UnsafeTargetError(
                f"{host} resolves to the private address {ip}. Expose it through "
                "a public hostname or a tunnel."
            )


def build_odoo_client(tenant: Tenant, conn: OdooConnection) -> OdooClient:
    assert_safe_url(conn.url)
    return OdooClient(
        OdooCredentials(
            url=conn.url,
            db=conn.db_name,
            username=conn.username,
            api_key=decrypt(conn.api_key_enc, tenant.crypto_key) or "",
            uid=conn.uid_cache,
            company_id=conn.company_id,
        )
    )


def build_source_provider(tenant: Tenant, source: DeviceSource) -> AttendanceProvider:
    """Construct the integration a source is configured to use.

    This is the single place the vendor is decided. Callers above hold an
    ``AttendanceProvider`` and never learn which one, which is what lets a tenant
    run BioTime at one site and something else at another.
    """
    assert_safe_url(source.base_url)

    options = dict(source.config or {})
    # Columns win over the JSON bag: they are what the connection form writes,
    # and a stale copy left in config must never quietly override them.
    options["auth_type"] = source.auth_type

    return build_provider(
        source.provider or "biotime",
        SourceConfig(
            base_url=source.base_url,
            username=source.username,
            password=decrypt(source.password_enc, tenant.crypto_key) or "",
            token=decrypt(source.token_enc, tenant.crypto_key),
            verify_ssl=source.verify_ssl,
            timezone=source.server_timezone,
            options=options,
        ),
    )
