# The Orders viewer is a single-page application, not server-rendered pages

The viewer is a React single-page application, written in TypeScript and built
with Vite, served as static assets by the same FastAPI process that answers its
JSON API. This repository is otherwise Python and `uv` only, so this introduces
a second language, a second package manager, a second lockfile, a Node build
stage in the image, and a type-check step in the pre-push checks.

Server-rendered templates were the alternative. They would deliver the two
screens this viewer has today — a health panel and an Order lookup — with about
three Python dependencies and no build stage at all. On the merits of those two
screens, templates win.

We chose the single-page application anyway, on a bet: that this becomes the
HawkExpress operations interface rather than staying at two screens. Orders,
Customers, Locations and driver activity are all things somebody will want to
look at, and the point at which templates stop being pleasant is somewhere just
past where we are now. Paying the cost once, at the beginning, is cheaper than
paying it as a rewrite later.

## Consequences

The cost is real and is not amortised by these two screens. It is a bet, and
this file exists so that a future reader finds a decision rather than drift.

If the screens that justify it do not materialise, reversing this costs the
`frontend/` directory and one build stage — the API stays as it is, because it
is a JSON API either way. That is the exit, and it is cheap; it stops being
cheap once anything else consumes the API.

The frontend has no tests, deliberately. The correctness risk in the viewer is
in the queries, in the serialisation of money and timestamps, and in the
viewer role's privileges — all of it below the HTTP boundary and all of it
tested there. A JavaScript test runner would be a third toolchain bought for
nothing. It gets added when a component holds logic worth asserting.

Type safety is only worth its cost if it is enforced. `npm run build` is
`tsc --noEmit && vite build`, and the image runs it, so a type error fails the
image rather than shipping in it. That is the only enforcement point today: the
pre-push checks still run `ruff` and `pytest` only, and until they type-check
too, a type error is caught at build time rather than before a push.
