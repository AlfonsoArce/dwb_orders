-- The dispatch-assigned Route Stop id is not unique. Drop the constraint.
--
-- 0002 enforced uniqueness on it, on the reasonable assumption that an
-- identifier assigned by the dispatch system identifies something. The
-- archive says otherwise: Orders 305804 and 305805 are different Orders —
-- different dispatch records, different Companies, placed nearly an hour
-- apart — and both of their Final Stops carry route_stop_id 295832.
--
-- Rejecting that would mean refusing to store an Order that genuinely
-- exists, so the identifier is kept as an ordinary indexed column. It is
-- still worth an index: it is how a stop is matched back to the dispatch
-- system. See ADR-0005.

drop index route_stops_dispatch_stop_id_key;

create index route_stops_dispatch_stop_id_idx
    on route_stops (source_key, dispatch_stop_id)
    where dispatch_stop_id is not null;
