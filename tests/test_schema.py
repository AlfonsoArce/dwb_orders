"""The Orders and Route Stops schema, tested by trying to break it.

These go at the database directly rather than through an entry point: a
constraint is only worth having if violating it fails, and the only way to
show that is to violate it.
"""

import psycopg
import pytest
from conftest import one, rows

ORDER_SQL = """
insert into orders (source_key, order_number, order_status, raw)
values ('digital_waybill', %s, %s, '{}'::jsonb)
returning order_id
"""

STOP_SQL = """
insert into route_stops (order_id, source_key, stop_position, dispatch_stop_id, raw)
values (%s, 'digital_waybill', %s, %s, '{}'::jsonb)
"""


def add_order(conn, order_number, status="Completed"):
    return one(conn, ORDER_SQL, (order_number, status))[0]


def add_stop(conn, order_id, position, dispatch_stop_id=None):
    with conn.cursor() as cur:
        cur.execute(STOP_SQL, (order_id, position, dispatch_stop_id))


def test_the_natural_key_rejects_a_duplicate_order_within_a_source(conn):
    add_order(conn, 401791)

    with pytest.raises(psycopg.errors.UniqueViolation):
        add_order(conn, 401791)


def test_the_same_order_number_from_another_source_is_allowed(conn):
    with conn.cursor() as cur:
        cur.execute("insert into sources (source_key, name) values ('other', 'Other')")
    add_order(conn, 401791)
    with conn.cursor() as cur:
        cur.execute("insert into orders (source_key, order_number, raw) "
                    "values ('other', 401791, '{}'::jsonb)")

    assert one(conn, "select count(*) from orders where order_number = 401791") == (2,)


def test_route_stops_are_unique_within_their_order_by_position(conn):
    order_id = add_order(conn, 401791)
    add_stop(conn, order_id, 1)

    with pytest.raises(psycopg.errors.UniqueViolation):
        add_stop(conn, order_id, 1)


def test_several_stops_without_a_dispatch_identifier_coexist(conn):
    first = add_order(conn, 401791)
    second = add_order(conn, 401792)
    add_stop(conn, first, 1, dispatch_stop_id=None)
    add_stop(conn, second, 1, dispatch_stop_id=None)

    assert one(conn, "select count(*) from route_stops where dispatch_stop_id is null") == (2,)


def test_two_stops_sharing_a_dispatch_identifier_are_both_stored(conn):
    # The archive contains 23 collisions between Orders that are genuinely
    # different, so this identifier cannot be unique. See ADR-0005.
    first = add_order(conn, 305804)
    second = add_order(conn, 305805)

    add_stop(conn, first, 2, dispatch_stop_id=295832)
    add_stop(conn, second, 2, dispatch_stop_id=295832)

    assert one(conn, "select count(*) from route_stops where dispatch_stop_id = 295832") \
        == (2,)


def test_deleting_an_order_removes_its_route_stops(conn):
    order_id = add_order(conn, 401791)
    add_stop(conn, order_id, 1)
    add_stop(conn, order_id, 2, dispatch_stop_id=391806)

    with conn.cursor() as cur:
        cur.execute("delete from orders where order_id = %s", (order_id,))

    assert one(conn, "select count(*) from route_stops") == (0,)


def test_a_stop_cannot_belong_to_an_order_from_another_source(conn):
    order_id = add_order(conn, 401791)
    with conn.cursor() as cur:
        cur.execute("insert into sources (source_key, name) values ('other', 'Other')")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute("insert into route_stops (order_id, source_key, stop_position, raw) "
                        "values (%s, 'other', 1, '{}'::jsonb)", (order_id,))


def test_money_columns_are_exact_decimals(conn):
    types = dict(rows(conn, """
        select column_name, data_type from information_schema.columns
        where table_name = 'orders' and column_name in ('price', 'final_price')
    """))

    assert types == {"price": "numeric", "final_price": "numeric"}


def test_every_timestamp_column_is_timezone_aware(conn):
    naive = rows(conn, """
        select table_name, column_name from information_schema.columns
        where table_name in ('orders', 'route_stops')
          and data_type = 'timestamp without time zone'
    """)

    assert naive == []


def test_an_order_records_the_timezone_assumption_applied_to_it(conn):
    order_id = add_order(conn, 401791)

    assert one(conn, "select timezone_assumption from orders where order_id = %s",
               (order_id,)) == ("America/New_York",)


def test_the_fields_ADR_0004_excludes_have_no_typed_column(conn):
    columns = {(t, c) for t, c in rows(conn, """
        select table_name, column_name from information_schema.columns
        where table_name in ('orders', 'route_stops')
    """)}

    excluded = {("orders", "comm_override"),      # packs several values into one string
                ("route_stops", "cancel_date"),   # populated on Orders never cancelled
                ("route_stops", "distance"),      # units unconfirmed
                ("route_stops", "air_distance"),
                ("route_stops", "route_status"),  # undocumented numeric code
                ("route_stops", "stop_status")}
    assert columns & excluded == set()


def test_both_tables_retain_the_raw_payload(conn):
    types = dict(rows(conn, """
        select table_name, data_type from information_schema.columns
        where table_name in ('orders', 'route_stops') and column_name = 'raw'
    """))

    assert types == {"orders": "jsonb", "route_stops": "jsonb"}


def test_the_location_index_includes_the_suite(conn):
    location_indexes = [d for _, d in rows(conn, """
        select indexname, indexdef from pg_indexes
        where tablename = 'route_stops' and indexdef like '%company%'
    """)]

    assert location_indexes, "no index covers Location"
    assert all("suite" in d for d in location_indexes)


def test_terminal_status_is_derived_from_order_status(conn):
    add_order(conn, 1, "Completed")
    add_order(conn, 2, "Cancelled")
    add_order(conn, 3, "Confirmed")   # In Flight, per the spec's assumption
    add_order(conn, 4, "Dispatched")
    add_order(conn, 5, None)          # no status at all is not Terminal either

    in_flight = rows(conn, "select order_number from orders where not is_terminal "
                           "order by order_number")
    assert in_flight == [(3,), (4,), (5,)]
