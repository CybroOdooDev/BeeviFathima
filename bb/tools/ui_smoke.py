#!/usr/bin/env python3
"""Drive the dashboard in a real browser and assert it works.

Static checks cannot catch a runtime JS error, an unresolved import or a handler
that never fires. This signs up, connects both sides, runs a sync and reads the
rendered result — failing on any console error along the way.

    python3 tools/ui_smoke.py --base http://127.0.0.1:8010 \
        --odoo-url http://127.0.0.1:8069 --odoo-db mydb \
        --odoo-user admin --odoo-key admin
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright

RESULTS: list[tuple[str, bool]] = []
CONSOLE_ERRORS: list[str] = []


def check(label, condition, detail=""):
    RESULTS.append((label, bool(condition)))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--odoo-url", required=True)
    parser.add_argument("--odoo-db", required=True)
    parser.add_argument("--odoo-user", required=True)
    parser.add_argument("--odoo-key", required=True)
    parser.add_argument("--biotime", default="http://127.0.0.1:8099")
    parser.add_argument("--shot", default="/tmp/bb_ui.png")
    parser.add_argument(
        "--shot-dir",
        default=None,
        help="Also save one screenshot per screen here. Worth setting when a "
             "check fails: the picture usually says what the assertion cannot.",
    )
    parser.add_argument(
        "--chromium",
        default=None,
        help="Path to a Chromium binary, when the bundled one does not match "
             "the installed Playwright build.",
    )
    args = parser.parse_args()

    launch_kwargs = {"args": ["--no-sandbox"]}
    if args.chromium:
        launch_kwargs["executable_path"] = args.chromium

    shot_dir = None
    if args.shot_dir:
        shot_dir = pathlib.Path(args.shot_dir)
        shot_dir.mkdir(parents=True, exist_ok=True)

    def snap(page, name):
        if shot_dir:
            page.screenshot(path=str(shot_dir / f"{name}.png"), full_page=True)

    email = f"ui{int(time.time())}@example.com"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: CONSOLE_ERRORS.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: CONSOLE_ERRORS.append(str(e)))

        # --- sign up --------------------------------------------------------
        print("\n--- sign up ---")
        page.goto(f"{args.base}/app/", wait_until="networkidle")
        check("the app loads", page.locator(".brand").first.is_visible())
        snap(page, "01-login")
        page.click("text=Create one")
        page.wait_for_timeout(400)
        snap(page, "02-signup")
        page.fill("#company_name", "UI Smoke Co")
        page.fill("#email", email)
        page.fill("#password", "a-long-enough-password")
        page.fill("#timezone", "Asia/Dubai")
        page.click("#go")
        page.wait_for_selector("#sidenav a", timeout=15000)
        check("signup lands on the dashboard shell", page.locator("#sidenav").is_visible())
        # This account is an ordinary customer, so the cross-tenant console must
        # not be offered to it. Server-side checks are the real control; this
        # catches the nav filter silently inverting.
        check(
            "an ordinary customer is not shown the platform console",
            "All accounts" not in page.locator("#sidenav").inner_text(),
            page.locator("#sidenav").inner_text().replace("\n", " · "),
        )
        check("the overview renders stat tiles", page.locator(".stat").count() >= 4,
              f"{page.locator('.stat').count()} tiles")
        check("it warns that nothing is connected",
              "Finish connecting" in page.content())

        # --- connections ----------------------------------------------------
        print("\n--- connections ---")
        page.click("#sidenav >> text=Connections")
        page.wait_for_selector("#odooForm", timeout=10000)
        check("the connections page renders both forms",
              page.locator("#odooForm").is_visible() and page.locator("#sourceForm").is_visible())
        check("the Odoo URL field warns about the path suffix",
              "no /odoo or /web on the end" in page.content())
        check("the timezone field warns it shifts attendance",
              "shifts every attendance record by hours" in page.content())
        snap(page, "03-connections-empty")

        page.fill("#odooForm #url", args.odoo_url)
        page.fill("#odooForm #db_name", args.odoo_db)
        page.fill("#odooForm #username", args.odoo_user)
        page.fill("#odooForm #api_key", args.odoo_key)
        page.click("#saveOdoo")
        page.wait_for_selector("#testOdoo", timeout=20000)
        check("Odoo saved and reports connected",
              "connected" in page.locator("#odooForm").locator("xpath=..").inner_text().lower())

        page.fill("#sourceForm #base_url", args.biotime)
        page.fill("#sourceForm #username", "mock")
        page.fill("#sourceForm #password", "mock")
        page.fill("#sourceForm #server_timezone", "Asia/Dubai")
        page.click("#saveSource")
        page.wait_for_selector("#discover", timeout=20000)
        check("device platform saved", page.locator("#discover").is_visible())

        page.click("#discover")
        page.wait_for_timeout(2500)
        check("terminals imported", page.locator("[data-toggle]").count() >= 1,
              f"{page.locator('[data-toggle]').count()} device(s)")
        snap(page, "04-connections-connected")

        # --- sync -----------------------------------------------------------
        print("\n--- sync ---")
        page.click("#sidenav >> text=Overview")
        page.wait_for_selector("#syncNow", timeout=10000)
        check("Sync now is enabled once both sides are connected",
              page.locator("#syncNow").is_enabled())
        page.click("#syncNow")
        page.wait_for_timeout(5000)
        check("a run is recorded", "Last sync" in page.content())

        # --- the read screens -----------------------------------------------
        print("\n--- data screens ---")
        for index, (label, marker) in enumerate([
            ("Employees", "Matched badges"),
            ("Attendance", "From"),
            ("Activity", "Punch ledger"),
            ("Settings", "Pairing"),
        ], start=5):
            page.click(f"#sidenav >> text={label}")
            page.wait_for_timeout(1800)
            check(f"{label} renders", marker in page.content())
            snap(page, f"{index:02d}-{label.lower()}")

        # --- the punch ledger's filters -------------------------------------
        print("\n--- punch ledger filters ---")
        page.click("#sidenav >> text=Activity")
        page.wait_for_selector("#punchFilters", timeout=10000)
        options = page.locator("#terminal_sn option").count()
        check("the device filter is populated from the imported terminals",
              options >= 2, f"{options} option(s) including 'any device'")

        # Each run links to the punches it brought in — the answer to "what did
        # this sync fetch", which the counters alone cannot give.
        run_link = page.locator("a.link.sm").filter(has_text="punch").first
        if run_link.count():
            run_link.click()
            page.wait_for_timeout(2000)
            check("a run links to the punches it ingested",
                  "run_id=" in page.evaluate("location.hash")
                  and "Read" in page.content(),
                  page.evaluate("location.hash"))
            page.click("text=Show the whole ledger")
            page.wait_for_timeout(1500)
        else:
            check("a run links to the punches it ingested", False,
                  "no run reported new punches, so there was no link to click")

        serial = page.locator("#terminal_sn option").nth(1).get_attribute("value")
        page.select_option("#terminal_sn", serial)
        page.click("#applyPunches")
        page.wait_for_timeout(2000)
        check("filtering by device puts it in the URL, so the view can be shared",
              f"terminal_sn={serial}" in page.evaluate("location.hash"),
              page.evaluate("location.hash"))
        rows = page.locator("table").last.locator("tbody tr").count()
        shown = page.locator("table").last.locator("tbody tr td:nth-child(5)").all_inner_texts()
        check("only that device's punches are listed",
              all(serial in text for text in shown) if shown else True,
              f"{rows} row(s), all {serial}" if shown else "no punches in range")

        # A date range that cannot contain anything, to prove it filters rather
        # than being ignored.
        page.fill("#date_from", "2001-01-01")
        page.fill("#date_to", "2001-01-02")
        page.click("#applyPunches")
        page.wait_for_timeout(2000)
        check("an empty date range returns nothing rather than everything",
              "No punches" in page.content(), "empty state shown")
        page.click("text=Clear")
        page.wait_for_timeout(1500)
        check("Clear drops the filters", page.evaluate("location.hash") in ("#/activity", "#/"),
              page.evaluate("location.hash"))

        # --- the schedule ---------------------------------------------------
        print("\n--- schedule ---")
        page.click("#sidenav >> text=Overview")
        page.wait_for_selector("#syncNow", timeout=10000)
        body = page.content()
        check(
            "the overview reports the schedule state",
            "Next sync" in body or "Automatic sync is not running" in body,
            "scheduler strip rendered",
        )

        # A select yields a string, so "false" would PATCH as truthy and the
        # setting would appear to save and then come back On.
        page.click("#sidenav >> text=Settings")
        page.wait_for_selector("#sync_enabled", timeout=10000)
        page.select_option("#sync_enabled", "false")
        page.click("#save")
        page.wait_for_timeout(2500)
        page.click("#sidenav >> text=Overview")
        page.wait_for_timeout(1200)
        page.click("#sidenav >> text=Settings")
        page.wait_for_selector("#sync_enabled", timeout=10000)
        check(
            "turning automatic sync off actually persists",
            page.locator("#sync_enabled").input_value() == "false",
            f"reads back as {page.locator('#sync_enabled').input_value()}",
        )
        page.select_option("#sync_enabled", "true")
        page.click("#save")
        page.wait_for_timeout(2000)

        page.click("#sidenav >> text=Overview")
        page.wait_for_timeout(1500)
        snap(page, "09-overview")
        page.screenshot(path=args.shot, full_page=True)
        print(f"\nscreenshot: {args.shot}")

        browser.close()

    print("\n--- console ---")
    real = [e for e in CONSOLE_ERRORS if "favicon" not in e.lower()]
    check("no JavaScript errors", not real, "; ".join(real[:3]) if real else "clean")

    print("\n" + "=" * 62)
    failed = [label for label, ok in RESULTS if not ok]
    print(f"{len(RESULTS)} checks, {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
