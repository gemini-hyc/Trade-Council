"""Date normalisation for point-in-time checks.

Source files mix ``20261113``, ``2026-11-13`` and ``2026-11-13 15:00:00``. String
comparison across those formats is wrong ("20261113" > "2026-11-13" is true for
the wrong reason), so every comparison goes through here first.
"""

from __future__ import annotations

import re
from datetime import date, datetime

_DIGITS = re.compile(r"(\d{4})\D?(\d{2})\D?(\d{2})")


def to_iso_date(value: object) -> str:
    """Return ``YYYY-MM-DD`` or an empty string when the value is not a date."""

    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    match = _DIGITS.search(text)
    if not match:
        return ""
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return ""


def is_after(value: object, anchor: object) -> bool:
    """True when ``value`` is a date strictly later than ``anchor``."""

    left = to_iso_date(value)
    right = to_iso_date(anchor)
    return bool(left and right and left > right)
