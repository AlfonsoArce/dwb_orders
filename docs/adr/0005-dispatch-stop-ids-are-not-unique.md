# The dispatch-assigned Route Stop id is not unique

Route Stops carry the dispatch system's own `route_stop_id`. We enforced a
uniqueness constraint on it and the import of the existing archive failed
against real data, so the constraint is gone: the column is indexed, not
unique.

Orders 305804 and 305805 are two different Orders — different dispatch
records, different Companies, placed 53 minutes apart, one Completed and one
Cancelled — and both of their Final Stops carry `route_stop_id` 295832. There
are 23 such collisions across the 338,144 Route Stops in the archive, 46 rows
in total.

## Considered options

Treating the second Order as corrupt and dropping it was rejected outright:
both Orders exist, both were billed, and refusing to store one would lose data
to defend an assumption the vendor never made.

Making the identifier unique per Order rather than globally is what the
uniqueness would have meant if it held, but it does not add anything: stops
are already unique within their Order by position.

## Consequences

`route_stop_id` cannot be used to identify a Route Stop. Ours is
`route_stop_id` in our own schema — a surrogate — and the dispatch system's is
`dispatch_stop_id`, an ordinary indexed column useful for matching a stop back
to the dispatch system and nothing more. A First Stop still usually has none
at all.

Whether the collisions are a vendor bug or a reused counter has not been
confirmed with Digital Waybill.
