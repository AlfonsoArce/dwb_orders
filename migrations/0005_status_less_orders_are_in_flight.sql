-- An Order with no Order Status is In Flight, not neither.
--
-- is_terminal was `lower(order_status) in (...)`, which is NULL when the
-- status is NULL — and `where not is_terminal` then quietly skips those rows.
-- The Orders most likely to arrive without a status are exactly the ones the
-- sweep exists to chase, so they were invisible to it.

alter table orders drop column is_terminal;   -- takes its partial index with it

alter table orders add column is_terminal boolean generated always as (
    coalesce(lower(order_status) in ('completed', 'cancelled', 'canceled'), false)
) stored;

create index orders_in_flight_idx on orders (order_number) where not is_terminal;
