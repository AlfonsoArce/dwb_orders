"""Turning what Digital Waybill sends into what the columns hold.

Three rules, each with a reason:

* Timestamps arrive naive, in three different formats, from a server assumed
  to be in Miami. Every one is parsed and stamped America/New_York, and the
  assumption is recorded per Order so it can be corrected later without
  re-fetching (ADR-0001).
* Money is Decimal, never float. These are dollars that get invoiced.
* Empty strings become nulls. A column holding both is one every future query
  has to defend against.

Anything unparseable becomes null rather than raising: the value is still in
the retained raw payload, and one odd row must not stop an import of 169,000.
"""

import datetime as dt
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

# HawkExpress operates in Miami; the API sends no offsets (ADR-0001).
EASTERN = "America/New_York"

TIMESTAMP_FORMATS = (
    "%a, %d %b %Y %H:%M:%S",   # Tue, 25 Nov 2025 11:02:47
    "%m/%d/%Y %I:%M:%S %p",    # 11/25/2025 11:04:17 AM
    "%Y-%m-%d %H:%M:%S.%f",    # 2025-11-25 15:28:24.252 — the revision marker
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
)

TRUE_STRINGS = {"true", "t", "yes", "y", "1"}
FALSE_STRINGS = {"false", "f", "no", "n", "0", ""}


def text(value):
    """Return the value as text, with empty and whitespace-only becoming null."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


def timestamp(value, timezone=EASTERN):
    """Parse one of the API's naive timestamps into an aware datetime.

    The ambiguous hour on the autumn clock change resolves to the first
    occurrence (EDT), which is what fold=0 means (ADR-0001).
    """
    raw = text(value)
    if raw is None:
        return None
    for fmt in TIMESTAMP_FORMATS:
        try:
            # Naive on purpose: the API sends no offset, and the timezone is
            # attached below so the assumption is one line, not seven formats.
            naive = dt.datetime.strptime(raw, fmt)  # noqa: DTZ007
        except ValueError:
            continue
        return naive.replace(tzinfo=ZoneInfo(timezone), fold=0)
    return None


def money(value):
    """Parse a price into an exact Decimal. Never a float."""
    raw = text(value)
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


# Quantities are exact for the same reason prices are: they get multiplied.
quantity = money


def integer(value):
    """Parse an identifier or count into an int."""
    raw = text(value)
    if raw is None:
        return None
    try:
        return int(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None


def boolean(value):
    """Parse an Order Flag, which arrives as a bool, an int, or a string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in TRUE_STRINGS:
        return True
    if raw in FALSE_STRINGS:
        return None if raw == "" else False
    return None
