# Fields we can't trust or interpret get no typed column

Several fields the Digital Waybill API returns are either undocumented or
demonstrably unreliable, so they are kept only in the retained raw payload and
given no typed column of their own. Reaching for one should be a deliberate
act.

The clearest case is the stop cancellation date, which is populated on Orders
that were never cancelled — usually echoing the stop's receive date, sometimes
carrying an unrelated date, occasionally one that precedes it. A typed column
would invite a query filtering on its presence, and that query would classify
essentially every Order as cancelled.

Also excluded on the same grounds: the commission override field, which packs
several values into one delimited string; the two distance fields, whose units
we have not confirmed; and the numeric stop status code, whose values are
undocumented.

## Consequences

Nothing is lost — the values remain in the raw payload. Any of these can be
promoted to a typed column once its meaning is confirmed with Digital Waybill.
