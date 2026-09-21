#!/usr/bin/env python3
"""Drive both sign-in doors in a real browser and assert they stay separate.

The API side of the split is covered by tests/test_login_separation.py. This is
for the half pytest cannot see: a route that was never registered, a nav group
that renders for the wrong session, a sign-out that drops a support engineer on
the customer login. Those are runtime JS behaviours, and they fail silently.

Needs no Odoo and no BioTime — only the app and a browser. It boots its own
server against a throwaway database, so it cannot touch a real one.

    python3 tools/login_doors_proof.py
    python3 tools/login_doors_proof.py --headed --shot /tmp/doors.png
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

#: Uncaught exceptions, which is what "the page is broken" actually looks like.
#: Not console errors: this proof deliberately provokes a refused sign-in, and
#: the browser logs every non-2xx response as a console error, so asserting on
#: those would fail on the feature working.
PAGE_ERRORS: list[str] = []

PASSWORD = "a-long-enough-password"
CUSTOMER = "owner@acme.example.com"
BOTH = "dual@acme.example.com"
STAFF_ONLY = "ops@platform.example.com"


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
        # No clock: this proof is about sign-in, and a scheduler ticking in the
        # background would only add noise to the console log we assert on.
        "SCHEDULER_MODE": "off",
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
    """Three accounts: a plain customer, a dual-role user, and staff-only."""
    import requests

    for email, company in ((CUSTOMER, "Acme"), (BOTH, "Globex")):
        response = requests.post(
            f"{base}/api/v1/auth/signup",
            json={"company_name": company, "email": email,
                  "password": PASSWORD, "timezone": "Asia/Dubai"},
            timeout=30,
        )
        response.raise_for_status()

    # The flag is only ever set on the server, so this is what grant_admin.py
    # does rather than an HTTP call.
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.core.security import hash_password
    from app.models import User, UserRole

    engine = create_engine(f"sqlite:///{db_path}")
    db = sessionmaker(bind=engine)()
    db.scalars(select(User).where(User.email == BOTH)).first().is_platform_admin = True
    db.add(User(tenant_id=None, email=STAFF_ONLY,
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


def sign_out(page) -> None:
    page.click("#signOut")
    page.wait_for_selector("#auth-root:not(.hidden)", timeout=15_000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--shot", default="/tmp/bb_doors.png")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = tempfile.mkdtemp(prefix="bb-doors-")
    db_path = os.path.join(tmp, "proof.db")

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

            print("\nThe customer door")
            sign_in(page, base, CUSTOMER, "customer")
            page.wait_for_selector("#app-root:not(.hidden)", timeout=20_000)
            check("an ordinary customer signs in", page.locator("#sidenav").is_visible())
            check("and is not shown the console",
                  "Platform" not in page.locator("#sidenav").inner_text())
            check("and is offered no link to its door",
                  page.locator("#consoleSwitch a").count() == 0)
            sign_out(page)
            check("signing out returns to the customer login",
                  "/login" in page.url and "staff" not in page.url, page.url)

            print("\nA staff account with no workspace of its own")
            sign_in(page, base, STAFF_ONLY, "customer")
            page.wait_for_selector("#authError:not(:empty)", timeout=15_000)
            refusal = page.locator("#authError").inner_text()
            check("is turned away from the customer door", bool(refusal), refusal)
            check("and told where to go instead", "staff console" in refusal.lower(),
                  refusal)

            print("\nThe console door")
            sign_in(page, base, STAFF_ONLY, "staff")
            page.wait_for_selector("#app-root:not(.hidden)", timeout=20_000)
            nav = page.locator("#sidenav").inner_text().upper()
            check("the same account signs in here", page.locator("#sidenav").is_visible())
            check("and lands on the console", "PLATFORM" in nav, nav.replace("\n", " · "))
            check("with no customer screens offered", "ATTENDANCE" not in nav,
                  nav.replace("\n", " · "))
            check("and the console is what rendered", "/platform" in page.url, page.url)
            page.screenshot(path=args.shot)
            sign_out(page)
            check("signing out returns to the CONSOLE door, not the customer one",
                  "staff/login" in page.url, page.url)

            print("\nSomeone who is both a customer and staff")
            sign_in(page, base, BOTH, "customer")
            page.wait_for_selector("#app-root:not(.hidden)", timeout=20_000)
            nav = page.locator("#sidenav").inner_text().upper()
            check("signs in to their own workspace", "ATTENDANCE" in nav)
            check("and the console is NOT in their nav", "PLATFORM" not in nav,
                  nav.replace("\n", " · "))
            check("but is offered as a separate session",
                  page.locator("#consoleSwitch a").count() == 1)

            page.locator("#consoleSwitch a").click()
            page.wait_for_selector("#form input[name=email]", timeout=15_000)
            check("that link reaches the console door rather than bouncing home",
                  "staff/login" in page.url, page.url)
            page.fill("#form input[name=email]", BOTH)
            page.fill("#form input[name=password]", PASSWORD)
            page.click("#go")
            page.wait_for_selector("#app-root:not(.hidden)", timeout=20_000)
            nav = page.locator("#sidenav").inner_text().upper()
            check("and signing in there swaps the hat rather than adding one",
                  "PLATFORM" in nav and "ATTENDANCE" not in nav,
                  nav.replace("\n", " · "))

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
