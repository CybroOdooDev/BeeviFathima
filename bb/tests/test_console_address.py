"""The staff console has its own address.

Staff used to reach the console through the customer login and then a second
sign-in. /admin/ now serves the dashboard as the console — the page reads its
own path to pick the door — so these only pin that the address exists and
serves the same app, and that it opens nothing by itself.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_the_console_address_serves_the_dashboard():
    client = TestClient(app)
    for path in ("/admin/", "/app/"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert '<div id="app-root"' in response.text


def test_the_console_address_without_a_slash_lands_on_it():
    response = TestClient(app).get("/admin", follow_redirects=False)
    assert response.status_code in (301, 307, 308)
    assert response.headers["location"].endswith("/admin/")


def test_the_console_serves_the_same_assets():
    client = TestClient(app)
    for asset in ("js/app.js", "js/theme.js", "css/app.css"):
        assert client.get(f"/admin/{asset}").content == client.get(f"/app/{asset}").content


def test_the_address_is_not_the_boundary():
    """A page at /admin/ is only HTML; the console's data still needs a staff token."""
    assert TestClient(app).get("/api/v1/admin/tenants").status_code == 401
