"""Is this address one we're willing to accept, right now, before it costs
us a database row?

Pydantic's ``EmailStr`` (used on the request schemas) checks syntax only — it
never looks the domain up. This does the second half: does the domain even
exist and accept mail. A signup form is the cheapest place in the whole
system for someone to type ``asdf@asdf`` or a typo'd company domain
(``gmial.com``), and the alternative is finding out from a bounced welcome
email or a "I never got my link" support ticket.
"""

from __future__ import annotations

from email_validator import EmailNotValidError, validate_email

from app.core.config import settings


class UngenuineEmailError(ValueError):
    """Syntactically an email, but not one this deployment will accept."""


def assert_genuine_email(email: str) -> str:
    """Validate and normalize ``email``. Returns the normalized address.

    Raises ``UngenuineEmailError`` with a message that is safe to show the
    person who typed it.

    ``check_deliverability`` performs a real MX/A lookup — see
    ``settings.verify_email_deliverability`` for why that is a policy switch
    rather than always on (it needs outbound DNS, which not every deployment
    and no test run should depend on).
    """
    try:
        result = validate_email(
            email, check_deliverability=settings.verify_email_deliverability
        )
    except EmailNotValidError as exc:
        raise UngenuineEmailError(str(exc)) from exc
    return result.normalized
