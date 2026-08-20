# Orders are keyed by a surrogate id plus (Source, Order Number)

Order Numbers are unique within Digital Waybill, so using one as the primary
key is the obvious move. We deliberately don't. Orders will later be ingested
from a second dispatch system with its own independent numbering, so each
Order carries a Source, and the natural key is the pair — while the primary
key is a surrogate we control.

## Considered options

Using Digital Waybill's own internal record id was considered and rejected: it
is just as vendor-assigned as the Order Number, sits in a numeric range a
second system would very likely overlap, and is not the number invoices and
reports actually reference. Swapping one vendor integer for another buys
nothing.

A composite natural key of (Source, Order Number) with no surrogate is
defensible and saves a column, but every child table would then carry two
columns in its foreign key.

## Consequences

Order Number remains a first-class indexed column, so joins to billing records
and existing reports are unaffected. Adding a second Source requires no schema
change and cannot collide by construction.
