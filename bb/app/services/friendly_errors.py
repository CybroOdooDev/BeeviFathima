"""Short, customer-facing wording for a failed connection test.

The technical text an integration raises ("HTTP 404 on /json/2/res.users/...")
is for us; the customer who clicked Test Connection needs to know what to check.
``friendly_odoo_message`` maps the failure to one short sentence and the full
detail goes to the server log.
"""

from __future__ import annotations

from app.integrations.odoo import OdooAuthError, OdooError


def friendly_odoo_message(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()

    if isinstance(exc, OdooAuthError) or "rejected the credentials" in low or "refused the authentication" in low:
        if "no database named" in low:
            return "Connection failed. Check the Database name."
        return "Login failed. Check the Database, Login and API Key."
    if "must start with http" in low:
        return "Check the Server URL. It must start with https://"
    if "no path" in low or "just the server address" in low:
        return "Check the Server URL. Enter only the site address, with no path after it."
    if "cannot resolve the host" in low:
        return "Connection failed. The Server URL could not be found. Check it for typos."
    if "tls" in low or "ssl" in low or "certificate" in low:
        return "Connection failed. The secure connection could not be made. Check the Server URL."
    if "web page instead of" in low:
        return "Connection failed. The Server URL does not look like an Odoo site."
    if "timed out" in low or "connection refused" in low or "unreachable" in low:
        return "Connection failed. The site did not respond. Check that it is running."
    if "http 404" in low or "no odoo api endpoint" in low or "could not reach odoo's api" in low:
        return "Connection failed. Check the Server URL, and that the Odoo site is active."
    if any(code in low for code in ("http 502", "http 503", "http 504", "bad gateway")):
        return "Connection failed. The Odoo site is not responding. It may be inactive or restarting."
    if isinstance(exc, OdooError):
        # Already plain and actionable ("This Odoo user cannot see company id 7")
        # reads better as it is; only technical-looking text is replaced.
        technical = ("http ", "/json", "/xmlrpc", "traceback", "keyerror", "fault", "errno", "<html")
        if len(text) <= 160 and not any(t in low for t in technical):
            return text
        return "Connection failed. Check your Odoo details and that the site is active."
    return "Connection failed. Please try again."
