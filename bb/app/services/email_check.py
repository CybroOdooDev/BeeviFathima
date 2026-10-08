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

import re

from email_validator import EmailNotValidError, validate_email

from app.core.config import settings


class UngenuineEmailError(ValueError):
    """Syntactically an email, but not one this deployment will accept."""


#: name@domain.tld — something before the @, then a dotted domain whose last
#: label is at least two characters. Cheap and local: it runs before any DNS
#: lookup, so a typo like ``you@yourcompanycom`` is answered at once instead of
#: waiting on a resolver.
_EMAIL_SHAPE = re.compile(r"^[^\s@]+@(?:[^\s@.]+\.)+[^\s@.]{2,}$")

#: A DNS lookup that has not answered by now is treated as a failure, so a
#: signup can never hang on a slow or unreachable resolver.
DNS_TIMEOUT_SECONDS = 5


def shape_problem(email: str) -> str | None:
    """Why ``email`` is not even shaped like an address, or None if it is."""
    value = (email or "").strip()
    if not value:
        return "Enter your email address."
    if value.count("@") != 1:
        return "An email address needs exactly one @, like you@yourcompany.com."
    local, domain = value.split("@")
    if not local:
        return "Add the part before the @, like you@yourcompany.com."
    if not domain:
        return "Add the domain after the @, like you@yourcompany.com."
    if "." not in domain:
        return (f"“{domain}” isn't a complete domain — it looks like a dot is missing "
                "(for example yourcompany.com).")
    if not _EMAIL_SHAPE.match(value):
        return "That doesn't look like a valid email address. Use the form you@yourcompany.com."
    return None


def assert_genuine_email(email: str) -> str:
    """Validate and normalize ``email``. Returns the normalized address.

    Raises ``UngenuineEmailError`` with a message that is safe to show the
    person who typed it.

    ``check_deliverability`` performs a real MX/A lookup — see
    ``settings.verify_email_deliverability`` for why that is a policy switch
    rather than always on (it needs outbound DNS, which not every deployment
    and no test run should depend on).
    """
    problem = shape_problem(email)
    if problem:
        raise UngenuineEmailError(problem)
    try:
        result = validate_email(
            email,
            check_deliverability=settings.verify_email_deliverability,
            timeout=DNS_TIMEOUT_SECONDS,
        )
    except EmailNotValidError as exc:
        raise UngenuineEmailError(str(exc)) from exc
    return result.normalized
