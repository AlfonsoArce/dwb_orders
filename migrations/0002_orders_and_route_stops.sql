-- Orders and their Route Stops.
--
-- Hybrid shape: typed, indexed columns for what reports actually filter on,
-- plus the complete API payload retained per row so that a field we chose not
-- to model is never lost.
--
-- Named for the domain glossary in CONTEXT.md, not for the API's field names:
-- "status" alone is ambiguous here, so Order Status, Stop Status and Order
-- Flags each say which they are. Every column carries the API field it came
-- from in a comment.
--
-- Deliberately absent, per ADR-0004: the order's commission override, the
-- stop's cancellation date, both stop distance fields, and the numeric stop
-- status code. They are unreliable or uninterpreted; they stay in raw.

create table orders (
    order_id            bigint generated always as identity primary key,

    -- Natural key: the dispatch system plus its Order Number (ADR-0002).
    source_key          text        not null references sources (source_key),
    order_number        bigint      not null,

    -- The dispatch system's own record id ("id"). Kept, never used to
    -- identify an Order.
    dispatch_record_id  bigint,

    placed_at           timestamptz,          -- "time"
    order_status        text,                 -- "status": Confirmed, Completed, ...
    status_at           timestamptz,          -- "status_date"
    status_detail       text,                 -- "status_detail"
    origin              text,                 -- "origin": how the Order reached us
    order_type          text,                 -- "order_type": Delivery, Pickup, ...

    -- Money, exact. Never floating point: these are dollars on an invoice.
    price               numeric(12, 2),       -- "price"
    final_price         numeric(12, 2),       -- "final_price"

    customer_number     text,                 -- "customer_number": identifies the Customer
    cost_center         text,                 -- "cost_center": the Customer's name, free text
    dispatch_driver     text,                 -- "dispatch_driver"

    ready_at            timestamptz,          -- "ready_time"
    deliver_by          timestamptz,          -- "deliver_by"

    -- Order Flags: read-state markers set by dispatch staff. Unrelated to
    -- Order Status.
    flag_pending        boolean,              -- "pending"
    flag_flagged        boolean,              -- "flagged"
    flag_read           boolean,              -- "read"

    recurring_name      text,                 -- "recurring_name"
    optimized_route     text,                 -- "optimized_route"

    -- The dispatch system's revision marker ("version"). An upsert applies
    -- only when the incoming revision is strictly newer, which is what makes
    -- re-runs idempotent and stops a backfill overwriting a live fetch.
    revision            timestamptz,

    -- Which timezone the naive API timestamps above were interpreted as
    -- (ADR-0001). Recorded per Order so a wrong assumption is a
    -- re-interpretation rather than a re-fetch.
    timezone_assumption text        not null default 'America/New_York',

    -- The complete payload as received, including the fields with no column.
    raw                 jsonb       not null,

    -- Terminal Status: Completed or Cancelled, and never changing again.
    -- Anything else is In Flight and worth re-checking. Derived rather than
    -- stored so it cannot disagree with order_status.
    is_terminal         boolean generated always as (
                            lower(order_status) in ('completed', 'cancelled', 'canceled')
                        ) stored,

    first_ingested_at   timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    constraint orders_source_order_number_key unique (source_key, order_number),
    -- Redundant on its own; it is the target of the Route Stop foreign key,
    -- which carries the Source so a second dispatch system's stop ids cannot
    -- collide with Digital Waybill's.
    constraint orders_order_id_source_key unique (order_id, source_key)
);

create index orders_order_number_idx on orders (order_number);
create index orders_customer_number_idx on orders (customer_number);
create index orders_order_status_idx on orders (order_status);
create index orders_placed_at_idx on orders (placed_at);
create index orders_ready_at_idx on orders (ready_at);
-- The stragglers ticket 09 sweeps: few rows, so a partial index stays small.
create index orders_in_flight_idx on orders (order_number) where not is_terminal;


create table route_stops (
    route_stop_id       bigint generated always as identity primary key,

    order_id            bigint      not null,
    source_key          text        not null,

    -- Visit order, 1-based. An Order almost always has two stops, but three
    -- and five occur, so nothing assumes a count.
    stop_position       integer     not null check (stop_position >= 1),

    -- The dispatch system's own stop id ("route_stop_id"). Absent on the
    -- First Stop, which carries only Location fields — hence nullable, with
    -- uniqueness enforced only where a value is present.
    dispatch_stop_id    bigint,

    -- Location: a Company at an address including its suite. One building
    -- routinely houses many Companies, so the suite is part of the identity.
    company             text,                 -- "company"
    address             text,                 -- "address"
    suite               text,                 -- "suite"
    city                text,                 -- "city"
    state               text,                 -- "state"
    postal_code         text,                 -- "postal_code"
    country             text,                 -- "country"

    contact_name        text,                 -- "contact"."name"
    contact_phone       text,                 -- "contact"."phone"

    service_type        text,                 -- "service_type": e.g. 16 ft box truck
    package             text,                 -- "package"
    number_of_pieces    numeric,              -- "number_of_pieces"
    weight              numeric,              -- "weight"
    vehicle             text,                 -- "vehicle"
    driver_number       text,                 -- "driver_number"
    driver_pricelist    text,                 -- "driver_pricelist"

    paper_waybill       text,                 -- "paper_waybill"
    special_instructions text,                -- "special_instructions"
    return_add          text,                 -- "return_add"
    dispatch_message    text,                 -- "dispatch_message"
    notes               text,                 -- "notes"
    reference           text,                 -- "reference"
    fuel_surcharge      text,                 -- "fuel_surcharge"

    signature_contact   text,                 -- "signature_contact"
    signature           text,                 -- "signature"

    -- Stop Status is a numeric code with undocumented values (ADR-0004), so
    -- only its human-readable detail and its timestamp get columns.
    stop_status_detail  text,                 -- "route_status_detail"
    stop_status_at      timestamptz,          -- "route_status_date"

    received_at         timestamptz,          -- "receive_date"
    dispatched_at       timestamptz,          -- "dispatch_date"
    picked_up_at        timestamptz,          -- "pickup_date"
    delivered_at        timestamptz,          -- "delivery_date"
    confirmed_at        timestamptz,          -- "confirm_date"

    raw                 jsonb       not null,

    constraint route_stops_order_position_key unique (order_id, stop_position),
    constraint route_stops_order_fk foreign key (order_id, source_key)
        references orders (order_id, source_key) on delete cascade
);

-- Only where present: every First Stop arrives without one, and several such
-- stops must coexist happily.
create unique index route_stops_dispatch_stop_id_key
    on route_stops (source_key, dispatch_stop_id)
    where dispatch_stop_id is not null;

create index route_stops_order_id_idx on route_stops (order_id);
-- Location, suite included: without it two Companies in one building merge.
create index route_stops_location_idx
    on route_stops (company, address, suite, city);
