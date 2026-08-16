"""Small shared helpers: date/money normalization and timestamps."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from dateutil import parser as dateparser


def now_iso() -> str:
    """UTC timestamp, ISO 8601, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# The portal mostly uses dd-mm-yyyy HH:MM. dateutil with dayfirst handles this
# plus the occasional variant. Returns ISO 8601 string or None.
def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    s = value.strip()
    if not s or s.upper() in {"NA", "N/A", "NIL", "-"}:
        return None
    try:
        dt = dateparser.parse(s, dayfirst=True)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    return dt.isoformat()


_MONEY_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def parse_money(value: str | None) -> float | None:
    """Parse an Indian-formatted currency string (e.g. '1,34,000') to float.

    Returns None for 'NA'/'Nil'/empty/non-numeric values.
    """
    if not value:
        return None
    s = value.strip()
    if not s or s.upper() in {"NA", "N/A", "NIL", "-"}:
        return None
    m = _MONEY_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def clean_ws(value: str | None) -> str | None:
    """Collapse the portal's heavy tab/newline padding into single spaces."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", value).strip()
    return s or None
