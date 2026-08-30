"""Writing Orders into the store.

One path in, used by both the archive importer and the fetcher: a batch of
Order payloads is streamed into staging tables and applied with two set-based
statements. Row-by-row inserts would make a 169,000-Order import take hours,
and having a second, simpler path for the fetcher would mean two sets of
coercion rules to keep honest.

The upsert is guarded by the dispatch system's revision marker: an incoming
Order is written only when its marker is strictly newer than the stored one.
An equal marker is a no-op, which is what makes re-running anything — a
resumed import, an overlapping page fetch, a sweep — cheap and safe. When an
Order is rewritten its Route Stops are replaced wholesale, because a stop that
vanished upstream must vanish here too.
"""

import collections

from psycopg.types.json import Jsonb

from dwb import coerce

DEFAULT_SOURCE = "digital_waybill"

# Columns staged for orders, with the type each is staged as. The target
# table's own definition lives in migrations/0002; these must name the same
# columns, and the insert below fails loudly if they ever stop doing so.
ORDER_STAGING = (
    ("source_key", "text"),
    ("order_number", "bigint"),
    ("dispatch_record_id", "bigint"),
    ("placed_at", "timestamptz"),
    ("order_status", "text"),
    ("status_at", "timestamptz"),
    ("status_detail", "text"),
    ("origin", "text"),
    ("order_type", "text"),
    ("price", "numeric"),
    ("final_price", "numeric"),
    ("customer_number", "text"),
    ("cost_center", "text"),
    ("dispatch_driver", "text"),
    ("ready_at", "timestamptz"),
    ("deliver_by", "timestamptz"),
    ("flag_pending", "boolean"),
    ("flag_flagged", "boolean"),
    ("flag_read", "boolean"),
    ("recurring_name", "text"),
    ("optimized_route", "text"),
    ("revision", "timestamptz"),
    ("timezone_assumption", "text"),
    ("raw", "jsonb"),
)

# Route Stops are staged against their Order's natural key and joined to the
# surrogate key on the way in. revision rides along only to pick a winner when
# one batch carries the same Order twice — sync conflict copies do that.
STOP_STAGING = (
    ("source_key", "text"),
    ("order_number", "bigint"),
    ("revision", "timestamptz"),
    ("stop_position", "integer"),
    ("dispatch_stop_id", "bigint"),
    ("company", "text"),
    ("address", "text"),
    ("suite", "text"),
    ("city", "text"),
    ("state", "text"),
    ("postal_code", "text"),
    ("country", "text"),
    ("contact_name", "text"),
    ("contact_phone", "text"),
    ("service_type", "text"),
    ("package", "text"),
    ("number_of_pieces", "numeric"),
    ("weight", "numeric"),
    ("vehicle", "text"),
    ("driver_number", "text"),
    ("driver_pricelist", "text"),
    ("paper_waybill", "text"),
    ("special_instructions", "text"),
    ("return_add", "text"),
    ("dispatch_message", "text"),
    ("notes", "text"),
    ("reference", "text"),
    ("fuel_surcharge", "text"),
    ("signature_contact", "text"),
    ("signature", "text"),
    ("stop_status_detail", "text"),
    ("stop_status_at", "timestamptz"),
    ("received_at", "timestamptz"),
    ("dispatched_at", "timestamptz"),
    ("picked_up_at", "timestamptz"),
    ("delivered_at", "timestamptz"),
    ("confirmed_at", "timestamptz"),
    ("raw", "jsonb"),
)

ORDER_COLUMNS = tuple(name for name, _ in ORDER_STAGING)
STOP_COLUMNS = tuple(name for name, _ in STOP_STAGING)

IngestResult = collections.namedtuple(
    "IngestResult", "orders_seen orders_written stops_written orders_unusable")


def _staging_ddl(name, spec):
    """The DDL for one staging table, built from its column spec."""
    columns = ", ".join(f"{col} {type_}" for col, type_ in spec)
    # Temp tables are unlogged by nature, so a bulk load into one costs no WAL;
    # on commit delete rows means every batch starts empty without a truncate.
    return (f"create temp table if not exists {name} ({columns}) "
            "on commit delete rows")


def ensure_staging(conn):
    """Create this session's staging tables if they aren't there yet."""
    with conn.cursor() as cur:
        cur.execute(_staging_ddl("staging_orders", ORDER_STAGING))
        cur.execute(_staging_ddl("staging_stops", STOP_STAGING))


def order_values(order, source_key=DEFAULT_SOURCE, timezone=coerce.EASTERN):
    """Return one staging row for an Order, coerced."""
    ts = lambda key: coerce.timestamp(order.get(key), timezone)
    return (
        source_key,
        coerce.integer(order.get("order_number")),
        coerce.integer(order.get("id")),
        ts("time"),
        coerce.text(order.get("status")),
        ts("status_date"),
        coerce.text(order.get("status_detail")),
        coerce.text(order.get("origin")),
        coerce.text(order.get("order_type")),
        coerce.money(order.get("price")),
        coerce.money(order.get("final_price")),
        coerce.text(order.get("customer_number")),
        coerce.text(order.get("cost_center")),
        coerce.text(order.get("dispatch_driver")),
        ts("ready_time"),
        ts("deliver_by"),
        coerce.boolean(order.get("pending")),
        coerce.boolean(order.get("flagged")),
        coerce.boolean(order.get("read")),
        coerce.text(order.get("recurring_name")),
        coerce.text(order.get("optimized_route")),
        ts("version"),
        timezone,
        Jsonb(order),
    )


def stop_values(stop, order, position, source_key=DEFAULT_SOURCE,
                timezone=coerce.EASTERN):
    """Return one staging row for a Route Stop, coerced."""
    ts = lambda key: coerce.timestamp(stop.get(key), timezone)
    contact = stop.get("contact") or {}
    return (
        source_key,
        coerce.integer(order.get("order_number")),
        coerce.timestamp(order.get("version"), timezone),
        position,
        coerce.integer(stop.get("route_stop_id")),
        coerce.text(stop.get("company")),
        coerce.text(stop.get("address")),
        coerce.text(stop.get("suite")),
        coerce.text(stop.get("city")),
        coerce.text(stop.get("state")),
        coerce.text(stop.get("postal_code")),
        coerce.text(stop.get("country")),
        coerce.text(contact.get("name")),
        coerce.text(contact.get("phone")),
        coerce.text(stop.get("service_type")),
        coerce.text(stop.get("package")),
        coerce.quantity(stop.get("number_of_pieces")),
        coerce.quantity(stop.get("weight")),
        coerce.text(stop.get("vehicle")),
        coerce.text(stop.get("driver_number")),
        coerce.text(stop.get("driver_pricelist")),
        coerce.text(stop.get("paper_waybill")),
        coerce.text(stop.get("special_instructions")),
        coerce.text(stop.get("return_add")),
        coerce.text(stop.get("dispatch_message")),
        coerce.text(stop.get("notes")),
        coerce.text(stop.get("reference")),
        coerce.text(stop.get("fuel_surcharge")),
        coerce.text(stop.get("signature_contact")),
        coerce.text(stop.get("signature")),
        coerce.text(stop.get("route_status_detail")),
        ts("route_status_date"),
        ts("receive_date"),
        ts("dispatch_date"),
        ts("pickup_date"),
        ts("delivery_date"),
        ts("confirm_date"),
        Jsonb(stop),
    )


def _copy(cur, table, columns, rows):
    """Stream rows into a staging table with COPY.

    COPY rather than executemany because a batch is hundreds of rows wide and
    the import is 169,000 Orders deep; the round trips are the cost.
    """
    placeholders = ", ".join(columns)
    with cur.copy(f"copy {table} ({placeholders}) from stdin") as copy:
        for row in rows:
            copy.write_row(row)


# The stop insert needs the Order's revision to pick a winner when a batch
# carries the same Order twice, so it is staged alongside and dropped here.
_STOP_INSERT_COLUMNS = tuple(
    c for c in STOP_COLUMNS if c not in ("order_number", "revision"))

UPSERT_ORDERS = """
insert into orders ({columns})
select {columns}
from (
    select distinct on (source_key, order_number) *
    from staging_orders
    order by source_key, order_number, revision desc nulls last
) incoming
on conflict (source_key, order_number) do update set
    {assignments},
    updated_at = now()
where excluded.revision is not null
  and (orders.revision is null or excluded.revision > orders.revision)
returning orders.order_id
"""

REPLACE_STOPS = """
insert into route_stops (order_id, {columns})
select o.order_id, {qualified}
from (
    select distinct on (source_key, order_number, stop_position) *
    from staging_stops
    order by source_key, order_number, stop_position, revision desc nulls last
) s
join orders o
  on o.source_key = s.source_key and o.order_number = s.order_number
where o.order_id = any(%s)
"""


def _upsert_orders_sql():
    """UPSERT_ORDERS with its column list filled in from ORDER_STAGING.

    Generated rather than written out so that adding a column to the staging
    spec cannot leave the statement quietly writing the old set.
    """
    columns = ", ".join(ORDER_COLUMNS)
    # Everything but the natural key is refreshed; first_ingested_at is not.
    assignments = ",\n    ".join(
        f"{c} = excluded.{c}" for c in ORDER_COLUMNS
        if c not in ("source_key", "order_number"))
    return UPSERT_ORDERS.format(columns=columns, assignments=assignments)


def _replace_stops_sql():
    """REPLACE_STOPS with its column list filled in, for the same reason.

    The Order's natural key and revision are staged but not inserted: they
    exist to find the Order row and to pick a winner within a batch.
    """
    columns = ", ".join(_STOP_INSERT_COLUMNS)
    qualified = ", ".join(f"s.{c}" for c in _STOP_INSERT_COLUMNS)
    return REPLACE_STOPS.format(columns=columns, qualified=qualified)


def ingest_orders(conn, orders, source_key=DEFAULT_SOURCE):
    """Write a batch of Order payloads; return what was actually written.

    Orders whose stored revision is already at least as new are left alone,
    and so are their Route Stops. The caller commits.

    An Order with no usable Order Number cannot be keyed, and is counted and
    dropped rather than allowed to abort the batch around it.
    """
    timezone = coerce.EASTERN
    seen = [o for o in orders if isinstance(o, dict)]
    orders = [o for o in seen if coerce.integer(o.get("order_number")) is not None]
    unusable = len(seen) - len(orders)
    if not orders:
        return IngestResult(len(seen), 0, 0, unusable)

    ensure_staging(conn)
    with conn.cursor() as cur:
        _copy(cur, "staging_orders", ORDER_COLUMNS,
              (order_values(o, source_key, timezone) for o in orders))
        stop_rows = []
        for order in orders:
            stops = order.get("route_stops") or []
            for position, stop in enumerate(stops, start=1):
                if isinstance(stop, dict):
                    stop_rows.append(
                        stop_values(stop, order, position, source_key, timezone))
        if stop_rows:
            _copy(cur, "staging_stops", STOP_COLUMNS, stop_rows)

        cur.execute(_upsert_orders_sql())
        written = [row[0] for row in cur.fetchall()]

        stops_written = 0
        if written:
            cur.execute("delete from route_stops where order_id = any(%s)", (written,))
            if stop_rows:
                cur.execute(_replace_stops_sql(), (written,))
                stops_written = cur.rowcount
        # Staging empties itself at commit; clear it now so a caller that
        # keeps the transaction open for several batches starts each clean.
        cur.execute("truncate staging_orders, staging_stops")

    return IngestResult(len(seen), len(written), stops_written, unusable)
