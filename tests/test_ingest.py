"""One Order, from the shape the API returns it in to typed rows.

Driven through the importer's command line, because that is the seam: what
goes in is Orders on disk, what comes out is rows, and everything between is
free to change.
"""

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest
from conftest import FINAL_STOP, FIRST_STOP, make_order, one, rows, write_order

EASTERN = ZoneInfo("America/New_York")


@pytest.fixture
def load(run_cli, database, archive):
    """Import whatever is in the archive; return the completed process."""

    def run(*args):
        result = run_cli("import_orders.py", "--input-dir", str(archive),
                         "--dsn", database, *args)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    return run


@pytest.fixture
def db(database):
    with psycopg.connect(database, autocommit=True) as conn:
        yield conn


def test_an_order_and_its_stops_are_stored_with_correct_types(archive, load, db):
    write_order(archive, make_order())

    load()

    order = one(db, """
        select source_key, order_number, dispatch_record_id, order_status,
               order_type, origin, customer_number, cost_center, dispatch_driver,
               flag_pending, flag_flagged, flag_read
        from orders
    """)
    assert order == ("digital_waybill", 401791, 391490, "Completed", "Pickup",
                     "TelephoneOrder", "235", "LUFTHANSA TECHNIK", "ENGE",
                     False, False, False)
    assert one(db, "select count(*) from route_stops") == (2,)


def test_prices_are_stored_as_exact_decimals(archive, load, db):
    write_order(archive, make_order(price="60.25", final_price=81.0))

    load()

    assert one(db, "select price, final_price from orders") == (Decimal("60.25"),
                                                                Decimal("81.00"))


def test_timestamps_are_eastern_and_the_assumption_is_recorded(archive, load, db):
    write_order(archive, make_order())

    load()

    placed_at, ready_at, assumption = one(
        db, "select placed_at, ready_at, timezone_assumption from orders")
    assert placed_at == dt.datetime(2025, 11, 25, 11, 2, 47, tzinfo=EASTERN)
    assert ready_at == dt.datetime(2025, 11, 25, 11, 2, 0, tzinfo=EASTERN)
    assert assumption == "America/New_York"


def test_empty_strings_are_stored_as_nulls(archive, load, db):
    write_order(archive, make_order())

    load()

    assert one(db, "select status_detail, recurring_name from orders") == (None, None)
    assert one(db, "select suite, contact_name from route_stops where stop_position = 1") \
        == (None, None)


def test_the_fields_ADR_0004_excludes_survive_only_in_the_raw_payload(archive, load, db):
    write_order(archive, make_order(comm_override="1|2|3"))

    load()

    order_raw, = one(db, "select raw from orders")
    assert order_raw["comm_override"] == "1|2|3"
    stop_raw, = one(db, "select raw from route_stops where stop_position = 2")
    assert stop_raw["cancel_date"] == FINAL_STOP["cancel_date"]
    assert stop_raw["route_status"] == "4"
    assert (stop_raw["distance"], stop_raw["air_distance"]) == ("0", "0")


def test_the_raw_payload_round_trips(archive, load, db):
    order = make_order()
    write_order(archive, order)

    load()

    stored, = one(db, "select raw from orders")
    assert stored == order


def test_an_order_with_more_than_two_stops_is_stored_in_visit_order(archive, load, db):
    middle = dict(FINAL_STOP, company="MIDDLE STOP", route_stop_id=391807)
    last = dict(FINAL_STOP, company="LAST STOP", route_stop_id=391808)
    write_order(archive, make_order(stops=[dict(FIRST_STOP), middle, last]))

    load()

    assert rows(db, "select stop_position, company from route_stops order by stop_position") \
        == [(1, "UNITED AIRLINES CARGO FLL"), (2, "MIDDLE STOP"), (3, "LAST STOP")]


def test_a_first_stop_without_a_dispatch_identifier_is_stored(archive, load, db):
    write_order(archive, make_order())

    load()

    assert one(db, "select dispatch_stop_id from route_stops where stop_position = 1") \
        == (None,)
    assert one(db, "select dispatch_stop_id from route_stops where stop_position = 2") \
        == (391806,)


def test_ingesting_the_same_order_twice_leaves_one_order_and_one_set_of_stops(
        archive, load, db):
    write_order(archive, make_order())

    load()
    load("--restart")

    assert one(db, "select count(*) from orders") == (1,)
    assert one(db, "select count(*) from route_stops") == (2,)


def test_an_equal_revision_changes_nothing(archive, load, db):
    write_order(archive, make_order())
    load()
    before = one(db, "select updated_at, order_status from orders")

    write_order(archive, make_order(status="Cancelled"))  # same version marker
    load("--restart")

    assert one(db, "select updated_at, order_status from orders") == before


def test_a_newer_revision_replaces_the_stored_order(archive, load, db):
    write_order(archive, make_order(status="Confirmed"))
    load()

    write_order(archive, make_order(status="Completed",
                                    version="2025-11-26 09:00:00.000"))
    load("--restart")

    assert one(db, "select order_status, is_terminal from orders") == ("Completed", True)


def test_an_older_revision_leaves_the_stored_order_untouched(archive, load, db):
    write_order(archive, make_order(status="Completed",
                                    version="2025-11-26 09:00:00.000"))
    load()

    write_order(archive, make_order(status="Confirmed",
                                    version="2025-11-01 09:00:00.000"))
    load("--restart")

    assert one(db, "select order_status from orders") == ("Completed",)


def test_a_newer_revision_replaces_the_whole_stop_set(archive, load, db):
    write_order(archive, make_order(stops=[dict(FIRST_STOP), dict(FINAL_STOP),
                                           dict(FINAL_STOP, company="THIRD",
                                                route_stop_id=391809)]))
    load()

    write_order(archive, make_order(version="2025-11-26 09:00:00.000"))
    load("--restart")

    assert rows(db, "select stop_position, company from route_stops order by stop_position") \
        == [(1, "UNITED AIRLINES CARGO FLL"), (2, "LUFTHANSA TECHNIK")]
