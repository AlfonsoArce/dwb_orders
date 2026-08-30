"""The Orders viewer: a read-only window on the Order store.

One service. It answers a small JSON API and, from the same origin, serves
the built single-page application that consumes it — so there is no
cross-origin configuration to get wrong and nothing to keep in step but one
port. See ADR-0006 for why there is a single-page application at all.

Three properties of this module are load-bearing, and all three are there for
the same reason: the viewer must be able to tell you that the database is
down.

It connects per request, never at import or at startup. A connection opened
at startup makes the service refuse to boot exactly when it is most needed,
and it makes the application untestable against anything but the real store.

It connects as the Orders viewer's own role, with the Orders viewer's own
password — never POSTGRES_PASSWORD, which belongs to the role that owns every
table. Read-only is a privilege the database enforces (migration 0006), not a
convention this file keeps.

The health endpoint answers successfully whether or not the database does. A
health check that fails when its dependency fails tells you less than one that
stays up and names the problem.

Hand-written SQL throughout, per ADR-0003.

Run from the repository root, where the wrapper lives:

    uv run python viewer.py                       # http://127.0.0.1:8083
    uv run python viewer.py --port 9000
    uv run python viewer.py --host 0.0.0.0        # what the container does
    uv run python viewer.py --assets frontend/dist
    uv run python viewer.py --dsn "postgresql://dwb_viewer@127.0.0.1:5434/dwb_orders"

The frontend has to be built first — cd frontend && npm install && npm run build
— or run `npm run dev` there, which serves it with hot reload and proxies the API
back here. Until either happens the root path says so rather than 404ing.

The viewer's role needs its own password in DWB_VIEWER_PASSWORD; it will not
borrow POSTGRES_PASSWORD. See migration 0006.
"""

import argparse
import datetime as dt
import os
import sys
from typing import Optional

import psycopg
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from dwb import db
from dwb.config import load_dotenv, resolve_viewer_dsn, resolve_viewer_password

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Where `npm run build` leaves the frontend. Absent while developing, when
# Vite serves it instead, and absent in the tests, which have no Node.
DEFAULT_ASSETS = os.path.join(REPO_ROOT, "frontend", "dist")

# Where a server run on the host listens. Not 5434 (Postgres), not 8081
# (Adminer), and deliberately not 8082, which is where docker-compose
# publishes the container: developing means running this alongside the
# compose stack, and the frontend's dev server proxies here.
DEV_PORT = 8083

# Everything the health panel shows, in one round trip.
#
# `last_written_at` is max(updated_at) — when this store last wrote a row.
# Deliberately not called an update: CONTEXT.md reserves Revision for the
# dispatch system's own marker, and these two answer different questions.
#
# `newest` is the highest Order Number rather than the latest placed_at:
# Digital Waybill issues them in sequence, so it is the same Order either way,
# and the Order Number is what tells you where the fetcher got to. Both counts
# and the ordering are served by indexes the schema already has, including the
# partial index on Orders that are not terminal.
HEALTH_SQL = """
with newest as (
    select order_number, placed_at
    from orders
    order by order_number desc
    limit 1
)
select
    (select count(*) from orders)                        as total_orders,
    (select count(*) from orders where not is_terminal)  as in_flight_orders,
    (select max(updated_at) from orders)                 as last_written_at,
    (select order_number from newest)                    as newest_order_number,
    (select placed_at from newest)                       as newest_placed_at
"""


# ---------------------------------------------------------------------------
# What crosses the wire
#
# Timestamps go out as UTC with an offset. The columns are timezone-aware and
# each Order records the assumption its naive source values were read under
# (ADR-0001), so the wire format carries no assumption of its own; the client
# formats for Miami, where every reader is.
# ---------------------------------------------------------------------------

class DatabaseHealth(BaseModel):
    """Whether the store answered, and what it said if it did not."""

    reachable: bool
    problem: Optional[str] = None


class NewestOrder(BaseModel):
    """The furthest the fetcher has got: the highest Order Number, and when it was placed."""

    order_number: int
    placed_at: Optional[str] = None


class StoreFigures(BaseModel):
    """What the store holds, as of the one query the health panel makes."""

    total_orders: int
    in_flight_orders: int
    newest_order: Optional[NewestOrder] = None
    last_written_at: Optional[str] = None


class Health(BaseModel):
    """The health endpoint's whole answer: whether the store could be read, and what it holds."""

    database: DatabaseHealth
    # Absent when the database could not be reached: no figures is different
    # from figures that happen to be zero.
    store: Optional[StoreFigures] = None


def as_utc(value):
    """Render a timestamp as UTC with an offset, or None."""
    if value is None:
        return None
    if value.tzinfo is None:  # pragma: no cover - the columns are timestamptz
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------

def create_app(dsn=None, assets=None):
    """Build the viewer, without connecting to anything.

    `dsn` overrides the viewer's configured connection string, which is what
    lets a test point the whole application at a disposable database. `assets`
    overrides where the built frontend is looked for.
    """
    dsn = resolve_viewer_dsn(dsn)
    assets = os.path.abspath(assets or DEFAULT_ASSETS)

    app = FastAPI(
        title="HawkExpress Orders viewer",
        description="Read-only. See docs/adr/0006-a-single-page-application.md",
        # No version prefix on the routes: there is exactly one consumer and
        # it ships inside the same image, so a version segment would be
        # ceremony that never changes. And no schema or docs routes — the API
        # should expose what a screen uses and nothing else, or it becomes an
        # unversioned public surface by accident.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    def store_connection():
        """Open one connection as the viewer's own role, for one request.

        The password is passed explicitly so db.connect() never falls back to
        POSTGRES_PASSWORD: borrowing the owning role's secret is how a
        read-only account stops being read-only.
        """
        return db.connect(dsn, password=resolve_viewer_password())

    @app.get("/api/health", response_model=Health)
    def health():
        """The state of the Order store, or why it could not be read."""
        try:
            with store_connection() as conn, conn.cursor() as cur:
                cur.execute(HEALTH_SQL)
                total, in_flight, last_written, newest_number, newest_placed = (
                    cur.fetchone())
        except (db.DatabaseUnavailable, psycopg.Error) as e:
            # Both failures answer the reader's one question — "could you read
            # the store?" — with a no. Refusing to connect is the common one;
            # a server that goes away mid-query, or a table the viewer's role
            # cannot select from, arrive here instead of as a 500.
            return Health(
                database=DatabaseHealth(reachable=False, problem=str(e).strip()))

        newest = None
        if newest_number is not None:
            newest = NewestOrder(
                order_number=newest_number, placed_at=as_utc(newest_placed))
        return Health(
            database=DatabaseHealth(reachable=True),
            store=StoreFigures(
                total_orders=total,
                in_flight_orders=in_flight,
                newest_order=newest,
                last_written_at=as_utc(last_written),
            ),
        )

    _serve_frontend(app, assets)
    return app


def _asset_file(root, path):
    """Return the file `path` names inside `root`, or None.

    A request path is attacker-controlled input even on loopback, so the
    resolved path is checked to be under the assets directory rather than
    trusted to be.
    """
    candidate = os.path.normpath(os.path.join(root, path.lstrip("/")))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def _serve_frontend(app, assets):
    """Serve the built frontend from the same origin as the API.

    Registered after the API routes, because the catch-all below matches
    anything. Unknown paths under /api are a missing endpoint and say so; every
    other unknown path is one of the viewer's own screens, which live in the
    client's router — reloading /orders/401791 must reach the application, not
    a 404.
    """
    index = os.path.join(assets, "index.html")

    if not os.path.isfile(index):
        @app.get("/", include_in_schema=False, response_class=PlainTextResponse)
        def unbuilt():
            """Say how to build the frontend, rather than 404 on the root path."""
            return (
                "The Orders viewer's frontend has not been built.\n\n"
                "  cd frontend && npm install && npm run build\n\n"
                "Or run `npm run dev` there for hot reload, which serves the "
                "frontend itself and proxies the API back here.\n")
        return

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        """Serve a built asset, or index.html so the client's router can route it."""
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"No endpoint /{path}")
        return FileResponse(_asset_file(assets, path) or index)


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def main(argv=None):
    """Serve the viewer from the command line, until interrupted."""
    load_dotenv()
    p = argparse.ArgumentParser(
        description="Serve the read-only Orders viewer.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=None,
                   help="Postgres connection string. Env: DWB_VIEWER_DSN")
    p.add_argument("--host", default="127.0.0.1",
                   help="Address to bind (default 127.0.0.1; the container "
                        "binds 0.0.0.0 and is published on loopback).")
    # Deliberately not read from DWB_VIEWER_PORT: that setting is the port
    # docker-compose *publishes* the container on, and the container itself is
    # told --port 8000 by the Dockerfile. One variable, one meaning.
    p.add_argument("--port", type=int, default=DEV_PORT,
                   help=f"Port to listen on (default {DEV_PORT})")
    p.add_argument("--assets", default=None,
                   help="Directory holding the built frontend "
                        "(default frontend/dist)")
    args = p.parse_args(argv)

    uvicorn.run(create_app(args.dsn, args.assets), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
