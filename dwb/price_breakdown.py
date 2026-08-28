"""Parsing the PriceBreakdown column that Digital Waybill exports.

The column packs every charge on an order into one string. Charges are
separated by ``~4``; within a charge each field is a ``~``-token whose first
character says what it is:

* ``5`` — three per charge, in order: quantity, unit rate, pricing code
  (observed codes: ``AP`` auto-priced base, ``M`` manual, ``AS`` auto
  surcharge).
* ``6`` — references: usually an empty marker followed by the order's DWB id;
  a few orders carry an extra token (e.g. ``ADP``) between them.

So ``Waiting Time~573~5.45~5M~6~6245530`` is 73 minutes at $0.45, manual,
on order 245530. A charge's amount is quantity x rate, and the sum over all
charges is the order's FinalPrice.

Numeric fields arrive as typed by dispatchers: some carry ``$``, thousands
commas, or a stray trailing ``+`` (all seen in History-2025-04-12.xlsx).
Those are stripped before Decimal parsing; anything still unparseable becomes
a null quantity/rate rather than raising, same policy as coerce.py — one odd
row must not stop a run of 126,000.

FinalPrice is stored rounded to cents while the computed sum can land on an
exact half cent (mileage rates x tenths of a mile), and the source system is
not consistent about which way a half cent rounds. Validation therefore
accepts a difference of up to half a cent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# |computed sum - FinalPrice| at or under this is a match: FinalPrice is
# rounded to cents and half-cent ties go either way in the source system.
TOLERANCE = Decimal("0.005")


@dataclass
class Charge:
    """One line of an order's price breakdown."""

    description: str
    quantity: Decimal | None = None
    rate: Decimal | None = None
    code: str | None = None
    refs: tuple[str, ...] = ()

    @property
    def amount(self):
        """Quantity x rate, or None when either failed to parse."""
        if self.quantity is None or self.rate is None:
            return None
        return self.quantity * self.rate

    def as_dict(self):
        """A JSON-safe dict; Decimals become strings so no cent is lost."""
        return {
            "description": self.description,
            "quantity": _text(self.quantity),
            "rate": _text(self.rate),
            "amount": _text(self.amount),
            "code": self.code,
            "refs": list(self.refs),
        }


@dataclass
class Breakdown:
    """Every charge parsed from one PriceBreakdown value."""

    charges: list[Charge] = field(default_factory=list)

    @property
    def total(self):
        """Sum of all charge amounts, or None if any charge is unparseable."""
        total = Decimal(0)
        for charge in self.charges:
            if charge.amount is None:
                return None
            total += charge.amount
        return total

    def matches(self, final_price):
        """Whether the summed charges reproduce the order's FinalPrice."""
        if self.total is None or final_price is None:
            return False
        return abs(self.total - Decimal(str(final_price))) <= TOLERANCE

    def as_dict(self, final_price=None):
        """A JSON-safe dict of every charge plus the total.

        Pass final_price to also record whether the charges sum to it.
        """
        result = {
            "charges": [charge.as_dict() for charge in self.charges],
            "total": _text(self.total),
        }
        if final_price is not None:
            result["final_price"] = str(final_price)
            result["matches_final_price"] = self.matches(final_price)
        return result


def _text(value):
    """A Decimal as its exact string form, passing None through."""
    return None if value is None else str(value)


def _number(raw):
    """Parse a quantity or rate as dispatchers type them: $, commas, +."""
    cleaned = raw.strip().replace("$", "").replace(",", "").rstrip("+")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _charge(description, fives, sixes):
    quantity = _number(fives[0]) if len(fives) > 0 else None
    rate = _number(fives[1]) if len(fives) > 1 else None
    code = fives[2] if len(fives) > 2 else None
    return Charge(description.strip(), quantity, rate, code, tuple(sixes))


def parse(text):
    """Parse a PriceBreakdown string into a Breakdown.

    Returns an empty Breakdown for a null or blank value. A token whose tag
    is not 4, 5, or 6 has never been observed; if one appears it is folded
    into the current charge's references so nothing is silently dropped.
    """
    if text is None or not str(text).strip():
        return Breakdown()
    tokens = str(text).split("~")
    charges = []
    description, fives, sixes = tokens[0], [], []
    for token in tokens[1:]:
        tag, value = token[:1], token[1:]
        if tag == "4":
            charges.append(_charge(description, fives, sixes))
            description, fives, sixes = value, [], []
        elif tag == "5":
            fives.append(value)
        elif tag == "6":
            sixes.append(value)
        else:
            sixes.append(token)
    charges.append(_charge(description, fives, sixes))
    return Breakdown(charges)
