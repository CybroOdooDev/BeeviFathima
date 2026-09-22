#!/usr/bin/env python3
"""Stop and restart one account from the console, in a real browser.

tests/test_subscription_gate.py proves the API side. This proves the part a
test client cannot see: that staff can work the gate in two clicks, and — the
half that actually generates support tickets — that the customer is *told*.

Before this feature the sidebar pill stayed green for a stopped account and the
overview blamed a switch in the customer's own Settings that was plainly still
on. Those are rendering decisions, so they can only be checked by rendering.

Needs no Odoo and no BioTime. It boots its own server against a throwaway
database, so it cannot touch a real one.

    python3 tools/gate_proof.py
    python3 tools/gate_proof.py --headed --shot /tmp/gate.png
"""

from __future__ import annotations

import argparse
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS: list[tuple[str, bool, str]] = []
PAGE_ERRORS: list[str] = []

PASSWORD = "a-long-enough-password"
CUSTOMER = "owner@acme.example.com"
STAFF = "ops@platform.example.com"
REASON = "unpaid invoice 4021"


def check(label: str, condition, detail: str = "") -> bool:
    ok = bool(condition)
    RESULTS.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    return ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def boot(db_path: str, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "JWT_SECRET": secrets.token_urlsafe(48),
        "MASTER_ENCRYPTION_KEY": secrets.token_urlsafe(48),
        "SCHEDULER_MODE": "off",
        # This proof seeds owner@acme.example.com — a real MX lookup would
        # reject it (.example is RFC 2606 reserved, no mail exchanger exists)
        # and this run has no network access to depend on regardless. See
        # app.services.email_check.
        "VERIFY_EMAIL_DELIVERABILITY": "false",
    }
    root = pathlib.Path(__file__).resolve().parent.parent
    subprocess.run([sys.executable, "tools/init_db.py"], cwd=root, env=env, check=True,
                   stdout=subprocess.DEVNULL)
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=root, env=env,
    )


def seed(base: str, db_path: str) -> None:
    import requests

    requests.post(
        f"{base}/api/v1/auth/signup",
        json={"company_name": "Acme", "email": CUSTOMER,
              "password": PASSWORD, "timezone": "Asia/Dubai"},
        timeout=30,
    ).raise_for_status()

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.core.crypto import encrypt
    from app.core.security import hash_password
    from app.models import DeviceSource, OdooConnection, Tenant, User, UserRole

    db = sessionmaker(bind=create_engine(f"sqlite:///{db_path}"))()
    tenant = db.scalars(select(Tenant).where(Tenant.slug == "acme")).first()
    # A device, so the account is one the scheduler would really pick up —
    # otherwise "not syncing" would be true for the wrong reason.
    db.add(DeviceSource(
        tenant_id=tenant.id, name="BioTime", base_url="https://bio.invalid",
        username="svc", password_enc=encrypt("pw", tenant.crypto_key),
        server_timezone="Asia/Dubai", is_active=True,
    ))
    # And Odoo, so "Sync now" is actually enabled — the button is greyed out
    # until both sides are connected, which would hide the refusal this proof is
    # here to see. Neither address is ever reached: the gate answers first.
    db.add(OdooConnection(
        tenant_id=tenant.id, name="Odoo", url="https://odoo.invalid",
        db_name="acme", username="bot",
        api_key_enc=encrypt("key", tenant.crypto_key), is_active=True,
    ))
    db.add(User(tenant_id=None, email=STAFF,
                hashed_password=hash_password(PASSWORD),
                role=UserRole.owner.value, is_platform_admin=True))
    db.commit()
    db.close()


def sign_in(page, base: str, email: str, door: str) -> None:
    page.goto(f"{base}/#/{'staff/login' if door == 'staff' else 'login'}")
    page.wait_for_selector("#form input[name=email]", timeout=15_000)
    page.fill("#form input[name=email]", email)
    page.fill("#form input[name=password]", PASSWORD)
    page.click("#go")
    page.wait_for_selector("#app-root:not(.hidden)", timeout=20_000)


def sign_out(page) -> None:
    page.click("#signOut")
    page.wait_for_selector("#auth-root:not(.hidden)", timeout=15_000)


def overview(page, base: str) -> None:
    page.goto(f"{base}/#/")
    page.wait_for_selector("#syncNow", timeout=20_000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--shot", default="/tmp/bb_gate.png")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    db_path = os.path.join(tempfile.mkdtemp(prefix="bb-gate-"), "proof.db")

    server = boot(db_path, port)
    try:
        if not wait_for(f"{base}/health"):
            print("the server never came up", file=sys.stderr)
            return 2
        seed(base, db_path)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            page = browser.new_page()
            page.on("pageerror", lambda e: PAGE_ERRORS.append(str(e)))

            print("\nBefore: a running account")
            sign_in(page, base, CUSTOMER, "customer")
            overview(page, base)
            check("the pill does not say stopped",
                  "stopped" not in page.locator("#tenantPill").inner_text().lower(),
                  page.locator("#tenantPill").inner_text())
            check("no deactivation banner", page.locator(".banner.bad").count() == 0)
            check("Sync now is available", page.locator("#syncNow").is_enabled())
            sign_out(page)

            print("\nStaff stop the account — two clicks and a reason")
            sign_in(page, base, STAFF, "staff")
            page.wait_for_selector("tr[data-tenant] .gate-stop", timeout=20_000)
            check("the console offers Deactivate on the row itself",
                  page.locator("tr[data-tenant] .gate-stop").count() == 1)

            page.locator("tr[data-tenant] .gate-stop").first.click()
            page.wait_for_selector(".gate-reason", timeout=10_000)
            check("one click asks rather than acts",
                  page.locator(".gate-reason").count() == 1
                  and page.locator("tr[data-tenant] .gate-stop").count() == 0)

            page.fill(".gate-reason", REASON)
            page.locator(".gate-commit").first.click()
            page.wait_for_selector("tr[data-tenant] .gate-start", timeout=20_000)
            gate_text = page.locator("tr[data-tenant] .gate").first.inner_text()
            check("the row now offers Activate", page.locator(".gate-start").count() >= 1)
            check("and shows the reason to staff", REASON in gate_text,
                  gate_text.replace("\n", " · "))
            check("the status column agrees",
                  "suspended" in page.locator("tr[data-tenant]").first.inner_text())
            # Scroll to the row before the shot: the console's header cards fill
            # the viewport, and a screenshot of them proves nothing about the
            # control this proof is about.
            page.locator("tr[data-tenant]").first.scroll_into_view_if_needed()
            page.screenshot(path=args.shot)
            sign_out(page)

            print("\nThe customer is told — this is the part that was missing")
            sign_in(page, base, CUSTOMER, "customer")
            overview(page, base)
            pill = page.locator("#tenantPill").inner_text()
            check("the pill stops claiming everything is fine", "stopped" in pill.lower(),
                  pill)
            banners = page.locator(".banner.bad").first.inner_text()
            check("a banner explains it", "not syncing" in banners.lower(),
                  banners.replace("\n", " · ")[:90])
            check("and says the records are still there",
                  "still here" in banners.lower() or "still visible" in banners.lower())
            check("the staff note is NOT shown to the customer",
                  REASON not in page.content())

            card = page.locator(".card").first.inner_text()
            check("the schedule card no longer blames their own Settings",
                  "turned off in Settings" not in card,
                  card.replace("\n", " · ")[:90])
            check("it says the account is stopped instead",
                  "stopped for this account" in card.lower(),
                  card.replace("\n", " · ")[:90])

            print("\nAnd the button does not work either")
            page.locator("#syncNow").click()
            page.wait_for_selector(".toast", timeout=15_000)
            toast = page.locator(".toast").first.inner_text()
            check("Sync now is refused with a reason", "suspended" in toast.lower(), toast)

            page.goto(f"{base}/#/settings")
            page.wait_for_selector("#form", timeout=20_000)
            settings = page.locator("#content").inner_text()
            check("Settings says so too, where they will go looking",
                  "stopped" in settings.lower())
            check("their own sync switch is untouched",
                  page.locator("#form select[name=sync_enabled]").input_value() == "true",
                  "a suspension that flipped their switch would look like their doing")

            print("\nStaff restore it")
            sign_out(page)
            sign_in(page, base, STAFF, "staff")
            page.wait_for_selector("tr[data-tenant] .gate-start", timeout=20_000)
            page.locator("tr[data-tenant] .gate-start").first.click()
            page.wait_for_selector("tr[data-tenant] .gate-stop", timeout=20_000)
            check("one click restores it", page.locator(".gate-stop").count() >= 1)
            sign_out(page)

            sign_in(page, base, CUSTOMER, "customer")
            overview(page, base)
            check("and the customer is back to normal",
                  page.locator(".banner.bad").count() == 0
                  and "stopped" not in page.locator("#tenantPill").inner_text().lower())

            browser.close()

        print()
        check("no uncaught JavaScript errors anywhere", not PAGE_ERRORS,
              "; ".join(PAGE_ERRORS[:3]))

    finally:
        server.terminate()
        server.wait(timeout=15)

    failed = [label for label, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed"
          + (f" — screenshot: {args.shot}" if os.path.exists(args.shot) else ""))
    if failed:
        print("\nFAILED:")
        for label in failed:
            print(f"  - {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
