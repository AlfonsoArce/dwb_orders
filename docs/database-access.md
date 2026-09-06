# Connecting to the HawkExpress Order store

For another project or agent that needs to read Orders out of this database.

This is a **production store holding real customer data** — 169,850 Orders
placed with HawkExpress (Miami, FL) between January 2021 and today, retrieved
from the Digital Waybill dispatch system and kept current by a poller that
fetches every five minutes.

Read it. Do not write to it. The rest of this document explains how, and what
the rows mean.

---

## 1. The connection

| | |
|---|---|
| Engine | PostgreSQL 18 |
| Database | `dwb_orders` |
| Host | the machine running the `dwb_orders` stack — `127.0.0.1` on that host, otherwise its LAN address (currently `192.168.4.215`) |
| Port | **5434** (not 5432 — 5432 and 5433 belong to the TMS and billing databases on that machine) |
| Role to use | **`dwb_viewer`** |
| Authentication | `scram-sha-256`, required for every non-container-local connection |
| Schema | `public` |

Connection string:

```
postgresql://dwb_viewer@192.168.4.215:5434/dwb_orders
```

Postgres is published on `0.0.0.0` so other machines can reach it, which means
the password is the only thing between the network and real customer data.
Keep it in your own secret store, not in source control, and expect the port
to be firewalled to specific hosts.

### Which role you want

There are two accounts. Use the read-only one.

| Role | Privileges | Use it for |
|---|---|---|
| `dwb_viewer` | `SELECT` on every table, nothing else | **any consuming project — this is you** |
| `dwb` | owns every table; full DDL and DML | ingest, migrations, and this repository only |

`dwb_viewer` is read-only in Postgres, not by convention: it holds no
`INSERT`, `UPDATE`, `DELETE`, `TRUNCATE` or `REFERENCES` grant, so a careless
statement fails at the server rather than at code review. A default privilege
is in place, so tables added by future migrations are readable too.

Do not connect as `dwb`, and do not authenticate `dwb_viewer` with the `dwb`
password. A read-only account using the owning account's secret is not a
second account.

### Getting the password

Ask the operator of the `dwb_orders` stack for the value of
`DWB_VIEWER_PASSWORD`. It is a different secret from `POSTGRES_PASSWORD`, it
is not in the repository, and it is not in any migration — migration `0006`
creates the role deliberately without one, and it is set by hand once:

```sql
alter role dwb_viewer password '...';
```

If the role exists but cannot log in, that command has not been run on this
cluster yet.

### Verify you can read

```bash
psql "postgresql://dwb_viewer@192.168.4.215:5434/dwb_orders" \
  -c "select count(*) from orders;"
```

`PGPASSWORD` works as usual, and the viewer role should fail this on purpose:

```bash
psql "$DSN" -c "delete from orders where false;"   # ERROR: permission denied
```

### From code

Python (this repo's stack — psycopg 3, no ORM):

```python
import os, psycopg

conn = psycopg.connect(
    "postgresql://dwb_viewer@192.168.4.215:5434/dwb_orders",
    password=os.environ["DWB_VIEWER_PASSWORD"],
    connect_timeout=10,
)
```

SQLAlchemy, if your project uses one:

```python
create_engine(
    "postgresql+psycopg://dwb_viewer:%s@192.168.4.215:5434/dwb_orders"
    % quote_plus(os.environ["DWB_VIEWER_PASSWORD"])
)
```

Node (`pg`):

```js
new Pool({
  host: "192.168.4.215", port: 5434, database: "dwb_orders",
  user: "dwb_viewer", password: process.env.DWB_VIEWER_PASSWORD,
})
```

Never log a connection string that carries a password. This repository strips
it before printing (`dwb/db.py:safe_dsn`); do the equivalent.

### If you are running inside the same Docker network

`127.0.0.1:5434` is the address on the *host* and means nothing inside a
container. Attach to the compose network and use the service name and the
internal port instead:

```
postgresql://dwb_viewer@db:5432/dwb_orders
```

### There is no HTTP API to use instead

The stack runs an Orders viewer on `127.0.0.1:8082`, but it exposes exactly
one endpoint — `GET /api/health` — with no OpenAPI schema and no docs routes,
serving a single screen. It is not an integration surface. Connect to
Postgres.

---

## 2. The schema

Seven tables in `public`. The shape is hybrid throughout: typed, indexed
columns for what reports filter and sum on, plus the complete source payload
kept per row in a `raw jsonb` column, so a field nobody modelled is never
lost.

```
sources ──< orders ──< route_stops
                └──< order_price_breakdowns ──< order_charges
```

### `orders` — one row per Order

| Column | Type | Meaning |
|---|---|---|
| `order_id` | bigint PK | surrogate id, ours. Join on this. |
| `source_key` | text FK | dispatch system; `'digital_waybill'` is the only one today |
| `order_number` | bigint | **the business identifier** — what invoices, reports and people mean by an Order |
| `dispatch_record_id` | bigint | Digital Waybill's own record id. Kept, never used to identify an Order |
| `placed_at` | timestamptz | when the Order was placed |
| `order_status` | text | `Confirmed`, `Dispatched`, `PickedUp`, `Completed`, `Cancelled` |
| `status_at`, `status_detail` | timestamptz, text | when the status last moved, and its human-readable detail |
| `origin` | text | how the Order reached HawkExpress (phone, web, …) |
| `order_type` | text | `Delivery`, `Pickup`, `Third-Party` |
| `price` | numeric(12,2) | the price first quoted |
| `final_price` | numeric(12,2) | **the amount billed** |
| `customer_number` | text | the stable Customer identifier |
| `cost_center` | text | the Customer's name as typed by dispatch — free text, contains typos |
| `dispatch_driver` | text | assigned driver |
| `ready_at`, `deliver_by` | timestamptz | the Order's window |
| `flag_pending`, `flag_flagged`, `flag_read` | boolean | Order Flags: whether a human has *looked* at the Order. Unrelated to Order Status |
| `recurring_name`, `optimized_route` | text | |
| `revision` | timestamptz | the dispatch system's change marker |
| `timezone_assumption` | text | which zone the naive source timestamps were read as; `America/New_York` |
| `raw` | jsonb | the complete API payload as received |
| `is_terminal` | boolean, generated | true when the Order is `Completed` or `Cancelled` |
| `first_ingested_at`, `updated_at` | timestamptz | store bookkeeping, not business events |

Unique: `(source_key, order_number)`. Indexed: `order_number`,
`customer_number`, `order_status`, `placed_at`, `ready_at`, and a partial
index on In Flight Orders.

### `route_stops` — the places an Order calls at

One row per stop, `stop_position` 1..n in visit order. Most Orders have two
stops; three and five occur, so **do not assume two**.

Location columns: `company`, `address`, `suite`, `city`, `state`,
`postal_code`, `country`, plus `contact_name` / `contact_phone`.

Operational columns: `service_type`, `package`, `number_of_pieces`, `weight`,
`vehicle`, `driver_number`, `driver_pricelist`, `paper_waybill`,
`special_instructions`, `return_add`, `dispatch_message`, `notes`,
`reference`, `fuel_surcharge`, `signature_contact`, `signature`.

Timestamps: `received_at`, `dispatched_at`, `picked_up_at`, `delivered_at`,
`confirmed_at`, plus `stop_status_detail` / `stop_status_at`.

Also `dispatch_stop_id` — the dispatch system's stop id, and **not unique**:
two genuinely different Orders can carry the same one (see ADR-0005). Never
key on it.

Unique: `(order_id, stop_position)`. Cascades on Order delete.

### `order_price_breakdowns` and `order_charges` — the itemised Final Price

The API reports Final Price as one number; the itemisation exists only in a
History export, so these tables are filled by a separate enrichment run.
**An Order no export has covered simply has no rows here** — treat their
absence as "not enriched", never as "no charges".

`order_price_breakdowns` (PK `order_id`): the exported `breakdown` string
verbatim, its `source_file`, the export's own `export_final_price`, and
`matches_final_price` — whether the parsed Charges sum to it within half a
cent.

`order_charges`: one row per Charge, `charge_position` 1..n, with
`description`, `quantity`, `rate`, `amount` (generated as `quantity * rate`),
`pricing_code` (`AP` auto-priced, `M` manual, `AS` auto surcharge), and
`refs jsonb`. Indexed on `description`, which is how reports group them.

### `sources`, `schema_migrations`, `import_progress`

`sources` is the dispatch-system lookup. The other two are this repository's
own bookkeeping — which migrations have run, and where a bulk import got to.
Readable, but of no interest to a consumer.

---

## 3. Rules of engagement

These are not style preferences; each one is a way to get a wrong answer.

1. **`SELECT` only.** The grants enforce it. Do not ask for more.
2. **An Order is identified by `(source_key, order_number)`.** `order_id` is
   ours and fine for joins; `dispatch_record_id` identifies nothing.
3. **A Location includes its suite.** One building routinely houses many
   Companies, so deduplicating or grouping on street address alone merges
   unrelated businesses. Group on `(company, address, suite, city)`.
4. **`cost_center` is free text, not a Customer identifier.** It is the
   Customer's name as a dispatcher typed it, typos included. Join on
   `customer_number`.
5. **Money is `numeric`.** Keep it in a decimal type end to end; never let it
   become a float.
6. **Timestamps are `timestamptz`, read as `America/New_York`.** The source
   sends no offsets; every value was parsed as Miami local time (ADR-0001).
   Convert explicitly for display: `placed_at at time zone 'America/New_York'`.
7. **Order Status and Order Flags are unrelated.** `flag_read` says a human
   looked at the Order; it says nothing about the job.
8. **Only `is_terminal` rows are settled.** Anything else is In Flight and the
   poller may still change it — including `final_price`. Do not cache an In
   Flight Order's price as final.
9. **`raw` is the escape hatch.** A field with no column is still there.
10. **Filter by `placed_at`, not by scanning.** Five years of Orders is a lot
    of rows; the useful indexes are listed above.

---

## 4. Queries to start from

Orders billed to one Customer in a month:

```sql
select order_number, placed_at, order_status, final_price, cost_center
from orders
where customer_number = $1
  and placed_at >= $2 and placed_at < $3
order by placed_at;
```

An Order with its stops in visit order:

```sql
select o.order_number, o.order_status, o.final_price,
       s.stop_position, s.company, s.address, s.suite, s.city, s.state,
       s.delivered_at
from orders o
join route_stops s on s.order_id = o.order_id
where o.source_key = 'digital_waybill' and o.order_number = $1
order by s.stop_position;
```

Revenue by Customer, completed Orders only:

```sql
select customer_number,
       count(*) as orders,
       sum(final_price) as billed
from orders
where order_status = 'Completed'
  and placed_at >= date_trunc('month', now() - interval '1 month')
group by customer_number
order by billed desc;
```

What a Final Price was made of, where enrichment has run:

```sql
select c.charge_position, c.description, c.quantity, c.rate,
       c.amount, c.pricing_code
from orders o
join order_charges c on c.order_id = o.order_id
where o.order_number = $1
order by c.charge_position;
```

Delivery volume by Location, suite included:

```sql
select company, address, suite, city, count(*) as stops
from route_stops
group by company, address, suite, city
order by stops desc
limit 50;
```

Is the store current?

```sql
select count(*) as total,
       count(*) filter (where not is_terminal) as in_flight,
       max(order_number) as newest_order,
       max(updated_at) as last_written
from orders;
```

`last_written` should be within a few minutes; the poller runs every 300
seconds.

---

## 5. Vocabulary

Use these words — they are what the business says, and what the columns are
named for. The full glossary is `CONTEXT.md` in the `dwb_orders` repository.

**Order** (not job/shipment/waybill) · **Order Number**, the business
identifier (not id) · **Order Status**, the lifecycle · **Terminal Status**,
Completed or Cancelled and never changing again; anything else is **In
Flight** · **Order Flags**, read-state markers, unrelated to status ·
**Revision**, the dispatch system's change marker · **Source**, the dispatch
system an Order came from (not to be confused with **Origin**, how the Order
reached HawkExpress) · **Route Stop**, with **First Stop** and **Final Stop**
named positionally · **Customer**, the account billed, identified by
**Customer Number** · **Company**, the business at a stop, often *not* the
Customer · **Location**, a Company at an address *including its suite* ·
**Final Price**, the amount billed · **Charge**, one line of it · **Price
Breakdown**, the full list · **Pricing Code**, how a rate was set.

Design decisions behind the schema are recorded in `docs/adr/0001` through
`0006` in the `dwb_orders` repository; ADR-0001 (timezones), ADR-0002 (keys),
ADR-0004 (why some fields stay in `raw`) and ADR-0005 (stop ids are not
unique) are the ones that change how you write a query.
