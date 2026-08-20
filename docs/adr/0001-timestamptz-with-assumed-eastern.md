# Timestamps are stored as `timestamptz`, assumed Eastern

Not one timestamp the Digital Waybill API returns carries a timezone offset,
and it sends three different formats. HawkExpress operates in Miami, so we
parse every incoming timestamp as `America/New_York` and store it as
`timestamptz`, recording the assumption per row so it can be audited and, if
necessary, corrected.

## Considered options

Storing naive `timestamp` values was the honest alternative — it asserts
nothing the API didn't tell us. We rejected it because a future Source may
supply real offsets, and a naive column would force us to discard information
we actually have. Postgres cannot mix naive and aware values in one column, so
this is a one-time, all-Sources decision.

Treating the strings as UTC was rejected outright: it would silently shift
every delivery time by four or five hours.

## Consequences

If Digital Waybill's server clock turns out not to be Eastern, every row is
wrong — which is why the assumption is recorded per row rather than left in
documentation, and why the original strings are retained. Correcting it is
then a re-interpretation, not a re-fetch.

The 1–2 AM hour on the autumn clock change is genuinely ambiguous; we resolve
it to the first occurrence (EDT). This affects a handful of rows per year.
