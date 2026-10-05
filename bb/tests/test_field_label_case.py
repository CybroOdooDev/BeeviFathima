"""Every form label starts each word with a capital letter — including the
labels providers declare in config_fields, which the connection form renders."""

from __future__ import annotations

import re

import app.integrations.providers  # noqa: F401 — registers every provider
from app.integrations.base import _REGISTRY


def _bad(label: str) -> list[str]:
    return [w for w in label.split() if re.match(r"[A-Za-z]", w) and w[0].islower()]


def test_every_provider_config_field_label_is_title_case():
    problems = {}
    for slug, cls in _REGISTRY.items():
        for field in getattr(cls, "config_fields", ()) or ():
            bad = _bad(str(field.get("label", "")))
            if bad:
                problems[f"{slug}.{field['name']}"] = field["label"]
    assert not problems, problems
