#!/usr/bin/env python3
"""Find out *which layer* of a device connection is broken, not just that it is.

"Connection refused" is where diagnosis starts, not where it ends. The address
can be unroutable, the port can be free because the service moved, the service
can be up but answering on https when you asked for http, the credentials can be
stale, or BioBridge can be in a container where ``localhost`` means the container
rather than the machine you are looking at. Those need opposite fixes and look
identical from the sync log.

So this walks the connection outward, one layer at a time, and stops at the
first thing that actually fails:

    URL  ->  DNS  ->  TCP  ->  TLS  ->  HTTP  ->  auth  ->  data

Usage
-----
    python3 tools/check_source.py                      # every active source
    python3 tools/check_source.py --tenant demo-company-2-2
    python3 tools/check_source.py --url http://localhost:8090   # no database

It reads the database directly rather than going through the API, for the same
reason show_users.py does: the reason you are running it is usually that
something is down, and a diagnostic that needs the healthy path is no
diagnostic. Nothing is written — it is safe to run against production.
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ports worth trying when the configured one is dead. BioTime's installer has
# used several defaults over the years and customers move it behind a proxy, so
# "wrong port" is a far commoner cause than "wrong host".
NEARBY_PORTS = [8090, 8081, 8000, 8080, 80, 443, 8099, 8888]

CONNECT_TIMEOUT = 3.0

OK = "  ok  "
BAD = " FAIL "
WARN = " warn "


@dataclass
class Report:
    """What each layer found, so the summary can explain rather than restate."""

    url: str
    label: str = ""
    lines: list[tuple[str, str]] = field(default_factory=list)
    failed_at: str | None = None
    advice: list[str] = field(default_factory=list)

    def step(self, status: str, message: str) -> None:
        self.lines.append((status, message))

    def fail(self, layer: str, message: str, *advice: str) -> None:
        self.step(BAD, message)
        self.failed_at = layer
        self.advice.extend(advice)


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
def in_container() -> bool:
    """Best-effort: is this process inside a container?

    Worth knowing because it changes what ``localhost`` means, which is the
    single most confusing failure in this whole area — the port is genuinely
    open on the machine, and genuinely closed from in here.
    """
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as handle:
            blob = handle.read()
        return any(marker in blob for marker in ("docker", "containerd", "kubepods", "lxc"))
    except OSError:
        return False


def port_open(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> tuple[bool, str, float]:
    """A raw TCP connect. No HTTP, no TLS — just: is anything accepting?"""
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "", time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 — every failure here is a finding
        return False, f"{type(exc).__name__}: {exc}", time.monotonic() - started


def scan_nearby(host: str, skip: int) -> list[int]:
    return [p for p in NEARBY_PORTS if p != skip and port_open(host, p, timeout=0.4)[0]]


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #
def check(url: str, username: str | None, password: str | None, label: str = "") -> Report:
    report = Report(url=url, label=label)

    # -- 1. the URL itself -------------------------------------------------
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        report.fail(
            "url", f"'{url}' has no http:// or https:// scheme",
            "Set the Server URL to something like http://10.0.0.9:8090",
        )
        return report

    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        report.fail("url", f"'{url}' has no hostname", "Check the Server URL field")
        return report

    report.step(OK, f"URL parses: {parsed.scheme}://{host}:{port}")

    if host in ("localhost", "127.0.0.1", "::1") and in_container():
        report.step(
            WARN,
            f"'{host}' from inside a container means the container itself, not "
            "the machine running it",
        )
        report.advice.append(
            "BioBridge is containerised, so localhost cannot reach a BioTime on "
            "the host. Use the host's LAN IP, or host.docker.internal, or put "
            "both on the same Docker network and use the service name."
        )

    # -- 2. DNS ------------------------------------------------------------
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addresses = sorted({info[4][0] for info in infos})
        report.step(OK, f"DNS resolves to {', '.join(addresses)}")
    except socket.gaierror as exc:
        report.fail(
            "dns", f"'{host}' does not resolve ({exc})",
            "Check the spelling of the hostname.",
            "If the server has no DNS entry, use its IP address instead.",
        )
        return report

    # -- 3. TCP ------------------------------------------------------------
    reachable, detail, elapsed = port_open(host, port)
    if not reachable:
        report.fail("tcp", f"nothing accepts a connection on {host}:{port} ({detail})")
        timed_out = "timed out" in detail.lower()
        if timed_out:
            report.advice.append(
                f"The connection timed out after {elapsed:.1f}s rather than "
                "being refused. That is packets being dropped — a firewall, or "
                "a VPN that is down — not a stopped process. A stopped service "
                "refuses instantly; only a filter makes you wait."
            )
            # Deliberately no port scan here. A filter drops every port equally,
            # so a scan either finds nothing (telling you what you already know)
            # or finds a false positive and sends you chasing the wrong port.
            report.advice.append(
                f"Check from the BioTime end instead: can that host see this "
                f"one at all? Confirm the service is up locally there, then open "
                f"port {port} to this host."
            )
            return report

        report.advice.append(
            "The connection was refused, which means the host is up and "
            "answered — nothing is listening on that port. The service is "
            "stopped, or it is on a different port."
        )
        others = scan_nearby(host, port)
        if others:
            report.advice.append(
                f"Something IS listening on {host}: port(s) "
                f"{', '.join(str(p) for p in others)}. If BioTime moved, point "
                "the Server URL at the right one."
            )
        elif host in ("localhost", "127.0.0.1", "::1"):
            report.advice.append(
                "Nothing is listening on any common port here. Start BioTime "
                "(or the mock: python3 tools/mock_biotime.py --port 8090), then "
                f"confirm with: ss -ltn | grep {port}"
            )
        return report

    report.step(OK, f"TCP connect to {host}:{port} in {elapsed * 1000:.0f} ms")

    # -- 4. TLS ------------------------------------------------------------
    if parsed.scheme == "https":
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
                with context.wrap_socket(sock, server_hostname=host) as tls:
                    cert = tls.getpeercert()
            report.step(OK, f"TLS handshake ok (expires {cert.get('notAfter', '?')})")
        except ssl.SSLCertVerificationError as exc:
            report.step(WARN, f"TLS certificate is not trusted: {exc.verify_message}")
            report.advice.append(
                "Self-signed certificates are normal on an on-premise BioTime. "
                "Untick 'Verify TLS certificate' on the connection, or install "
                "the certificate on this host."
            )
        except ssl.SSLError as exc:
            report.fail(
                "tls", f"TLS handshake failed: {exc}",
                "The port is open but not speaking TLS — it is probably plain "
                "http. Change https:// to http:// in the Server URL.",
            )
            return report

    # -- 5-7. HTTP, auth, data --------------------------------------------
    try:
        import httpx
    except ImportError:
        report.step(WARN, "httpx not installed — stopping before the HTTP checks")
        return report

    client = httpx.Client(
        base_url=f"{parsed.scheme}://{host}:{port}",
        timeout=10.0,
        verify=False,          # noqa: S501 — diagnosing, not trusting
        follow_redirects=True,
        trust_env=False,
    )
    try:
        try:
            root = client.get("/")
            server = root.headers.get("server", "unknown")
            report.step(OK, f"HTTP answers (HTTP {root.status_code}, server: {server})")
        except httpx.RequestError as exc:
            report.fail(
                "http", f"the port is open but HTTP failed: {type(exc).__name__}: {exc}",
                "Something is listening that is not a web server — check the port "
                "belongs to BioTime and not another service.",
            )
            return report

        if not username:
            report.step(WARN, "no credentials given — stopping before the auth check")
            return report

        try:
            auth = client.post(
                "/api-token-auth/", json={"username": username, "password": password or ""}
            )
        except httpx.RequestError as exc:
            report.fail("auth", f"auth request failed: {exc}")
            return report

        if auth.status_code == 404:
            report.fail(
                "auth", "no /api-token-auth/ endpoint here",
                "This is a web server, but not BioTime — check the URL and port.",
                "BioTime 8.5+ may want the JWT endpoint: set Auth style to 'jwt'.",
            )
            return report
        if auth.status_code in (400, 401, 403):
            report.fail(
                "auth", f"BioTime rejected the credentials (HTTP {auth.status_code})",
                "The server is reachable and is BioTime — only the username or "
                "password is wrong. Re-enter them on the connection.",
            )
            return report
        if auth.status_code >= 400:
            report.fail(
                "auth",
                f"auth returned HTTP {auth.status_code} ({_summarise(auth)})",
                "A 4xx/5xx that is not a credential rejection usually means "
                "this is some other web server on the port BioTime used to be "
                "on — a proxy, a dashboard, or a default page.",
                "Open the URL in a browser: BioTime shows its own login screen.",
            )
            return report

        try:
            payload = auth.json() or {}
        except ValueError:
            report.fail(
                "auth", f"auth returned HTML, not JSON ({_summarise(auth)})",
                "Something answered, but not BioTime's API. Check the port.",
            )
            return report

        token = payload.get("token") or payload.get("access")
        if not token:
            report.fail(
                "auth", "auth succeeded but returned no token",
                "If this is BioTime 8.5 or newer, set Auth style to 'jwt' on "
                "the connection — the token endpoint differs between versions.",
            )
            return report
        report.step(OK, "credentials accepted, token issued")

        txns = client.get(
            "/iclock/api/transactions/",
            params={"page": 1, "page_size": 1},
            headers={"Authorization": f"Token {token}"},
        )
        if txns.status_code >= 400:
            report.fail(
                "data", f"transactions endpoint returned HTTP {txns.status_code}",
                "Authentication works but this user cannot read transactions — "
                "check its permissions in BioTime.",
            )
            return report
        count = (txns.json() or {}).get("count", "?")
        report.step(OK, f"transactions readable — {count} punch(es) visible")
    finally:
        client.close()

    return report


# --------------------------------------------------------------------------- #
# Sources, from the database
# --------------------------------------------------------------------------- #
def load_sources(tenant_slug: str | None) -> list[tuple[str, str, str, str]]:
    """(tenant, source name, url, username) plus the decrypted password."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.core.crypto import decrypt
    from app.models import DeviceSource, Tenant

    engine = create_engine(settings.database_url)
    db = sessionmaker(bind=engine)()
    try:
        query = select(DeviceSource, Tenant).join(Tenant, DeviceSource.tenant_id == Tenant.id)
        if tenant_slug:
            query = query.where(Tenant.slug == tenant_slug)
        rows = db.execute(query).all()
        out = []
        for source, tenant in rows:
            if not source.is_active:
                continue
            out.append(
                (
                    tenant.slug,
                    source.name,
                    source.base_url,
                    source.username,
                    decrypt(source.password_enc, tenant.crypto_key) or "",
                )
            )
        return out
    finally:
        db.close()


def render(report: Report) -> None:
    header = f"{report.label}  —  {report.url}" if report.label else report.url
    print(f"\n{header}")
    print("-" * min(len(header), 76))
    for status, message in report.lines:
        print(f"  [{status}] {message}")

    if report.failed_at:
        print(f"\n  Broken at: {report.failed_at}")
    if report.advice:
        print("\n  What to do:")
        for item in report.advice:
            for i, chunk in enumerate(_wrap(item, 68)):
                print(f"    {'-' if i == 0 else ' '} {chunk}")
    if not report.failed_at and not report.advice:
        print("\n  This connection is healthy all the way through.")


def _summarise(response) -> str:
    """One short line describing a response body.

    An HTML error page pasted into a diagnostic buries the finding under the
    noise it was meant to replace, so tags are stripped and the result is cut to
    a phrase.
    """
    import re

    body = response.text or ""
    if "<html" in body[:200].lower():
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        text = title.group(1) if title else re.sub(r"<[^>]+>", " ", body)
    else:
        text = body
    text = " ".join(text.split())
    return (text[:80] + "…") if len(text) > 80 else (text or "empty body")


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose a device connection layer by layer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url", help="Check this URL instead of reading the database")
    parser.add_argument("--username", help="With --url")
    parser.add_argument("--password", help="With --url")
    parser.add_argument("--tenant", help="Only this tenant's sources (slug)")
    args = parser.parse_args()

    if in_container():
        print("note: running inside a container — 'localhost' here is the container.")

    if args.url:
        reports = [check(args.url, args.username, args.password)]
    else:
        try:
            sources = load_sources(args.tenant)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not read the database: {exc}", file=sys.stderr)
            print("Pass --url to check an address without it.", file=sys.stderr)
            return 2
        if not sources:
            where = f" for tenant '{args.tenant}'" if args.tenant else ""
            print(f"No active device sources found{where}.")
            return 1
        reports = [
            check(url, user, password, label=f"{slug} / {name}")
            for slug, name, url, user, password in sources
        ]

    for report in reports:
        render(report)

    broken = [r for r in reports if r.failed_at]
    print(f"\n{len(reports) - len(broken)} of {len(reports)} connection(s) healthy.")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
