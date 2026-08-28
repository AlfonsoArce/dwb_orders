"""The PriceBreakdown parser against every shape seen in the History export."""

from decimal import Decimal

from dwb.price_breakdown import Breakdown, Charge, parse


def test_single_charge():
    b = parse("Van AH: Ft. Lauderdale to Miami~51~574~5AP~6~6245458")
    assert len(b.charges) == 1
    charge = b.charges[0]
    assert charge.description == "Van AH: Ft. Lauderdale to Miami"
    assert charge.quantity == Decimal(1)
    assert charge.rate == Decimal(74)
    assert charge.code == "AP"
    assert charge.refs == ("", "245458")
    assert b.total == Decimal(74)
    assert b.matches(74)


def test_multiple_charges_sum_to_final_price():
    b = parse(
        "Van: Medley to Miami~51~568~5AP~6~6245466"
        "~4Waiting Time~51~515~5M~6~6245466"
        "~4Extra Stop~51~515~5M~6~6245466"
    )
    assert [c.description for c in b.charges] == [
        "Van: Medley to Miami",
        "Waiting Time",
        "Extra Stop",
    ]
    assert b.total == Decimal(98)
    assert b.matches(98)
    assert not b.matches(96)


def test_quantity_times_rate():
    # 73 minutes of waiting at $0.45 — quantity is not always 1.
    b = parse(
        "Cargo Van: Medley to Miami~51~565~5AP~6~6245530"
        "~4Waiting Time~573~5.45~5M~6~6245530"
    )
    assert b.charges[1].quantity == Decimal(73)
    assert b.charges[1].rate == Decimal("0.45")
    assert b.charges[1].amount == Decimal("32.85")
    assert b.total == Decimal("97.85")
    assert b.matches(Decimal("97.85"))


def test_dispatcher_typed_numbers():
    # Dollar signs, thousands commas, and a trailing + all appear in the wild.
    b = parse(
        "Base Price~51~51,115~5AP~6~6316204"
        "~4Waiting Time~551~5$0.45~5M~60~6316204"
        "~4Sprinter~51~575+~5AP~6~6316204"
    )
    assert b.charges[0].rate == Decimal(1115)
    assert b.charges[1].rate == Decimal("0.45")
    assert b.charges[2].rate == Decimal(75)


def test_extra_reference_token_is_kept():
    b = parse("DG~51~5$7.00~5M~6ADP~6318622")
    assert b.charges[0].refs == ("ADP", "318622")
    assert b.charges[0].amount == Decimal("7.00")


def test_half_cent_rounding_tolerance():
    # 50 + 15.5*1.25 + 15 + 45*0.45 = 104.625; the source stored 104.62.
    b = parse(
        "Van: 16.9mi.~51~550~5AP~6~6272200"
        "~4MLG RT~515.5~51.25~5M~6~6272200"
        "~4Extra Stop Van & Cargo Van~51~515~5M~6~6272200"
        "~4Waiting Time x Min~545~50.45~5M~6~6272200"
    )
    assert b.total == Decimal("104.625")
    assert b.matches(Decimal("104.62"))
    assert b.matches(Decimal("104.63"))
    assert not b.matches(Decimal("104.61"))


def test_unparseable_number_yields_null_not_raise():
    b = parse("Waiting Time~5abc~5.45~5M~6~6245530")
    assert b.charges[0].quantity is None
    assert b.charges[0].amount is None
    assert b.total is None
    assert not b.matches(15)


def test_blank_and_none():
    assert parse(None) == Breakdown()
    assert parse("   ") == Breakdown()
    assert parse("").total == Decimal(0)
    assert not parse(None).matches(10)


def test_as_dict_is_json_safe():
    import json

    b = parse(
        "Cargo Van: Medley to Miami~51~565~5AP~6~6245530"
        "~4Waiting Time~573~5.45~5M~6~6245530"
    )
    d = b.as_dict()
    assert json.loads(json.dumps(d)) == d  # round-trips as JSON
    assert d["total"] == "97.85"
    assert d["charges"][1] == {
        "description": "Waiting Time",
        "quantity": "73",
        "rate": "0.45",
        "amount": "32.85",
        "code": "M",
        "refs": ["", "245530"],
    }
    assert "final_price" not in d


def test_as_dict_with_final_price():
    b = parse("Van: Miami to Miami~51~565~5AP~6~6245465")
    assert b.as_dict("65")["matches_final_price"] is True
    assert b.as_dict("66")["matches_final_price"] is False
    # Unparseable amounts serialize as nulls, and never claim a match.
    d = parse("Waiting Time~5abc~5.45~5M~6~6245530").as_dict("15")
    assert d["total"] is None
    assert d["charges"][0]["quantity"] is None
    assert d["matches_final_price"] is False


def test_charge_amount_needs_both_fields():
    assert Charge("x", Decimal(2), None).amount is None
    assert Charge("x", None, Decimal(5)).amount is None
    assert Charge("x", Decimal(2), Decimal(5)).amount == Decimal(10)
