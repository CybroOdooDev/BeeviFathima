from app.integrations.odoo import OdooAuthError, OdooError
from app.services.friendly_errors import friendly_odoo_message as friendly


def test_the_screenshot_error_becomes_one_short_sentence():
    raw = OdooError("Could not reach Odoo's API at https://x.easyinstance.com: there is no Odoo API "
                    "endpoint there. The URL is wrong — most often it has a path on the end. "
                    "(HTTP 404 on /json/2/res.users/context_get)")
    msg = friendly(raw)
    assert "Server URL" in msg and "active" in msg
    assert "404" not in msg and "json/2" not in msg and len(msg) < 90


def test_each_common_failure_says_what_to_check():
    assert "Login failed" in friendly(OdooAuthError("Odoo rejected the credentials. The API reached Odoo"))
    assert "Database" in friendly(OdooAuthError("Odoo has no database named 'x'."))
    assert "could not be found" in friendly(OdooError("Cannot resolve the host in https://x: boom"))
    assert "did not respond" in friendly(OdooError("Timed out connecting to https://x. A firewall"))
    assert "did not respond" in friendly(OdooError("Connection refused by https://x. Nothing is listening there."))
    assert "inactive" in friendly(OdooError("... (HTTP 502 on /json/2/x/y)"))
    assert "secure connection" in friendly(OdooError("TLS failed for https://x: bad"))
    assert "no path" in friendly(OdooError("The Odoo URL must be just the server address, with no path.")).lower()


def test_unknown_errors_never_leak_internals():
    msg = friendly(OdooError("Traceback KeyError: 'foo' at line 12"))
    assert "Traceback" not in msg and msg.startswith("Connection failed")
