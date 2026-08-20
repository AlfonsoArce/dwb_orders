"""Test harness: a real, disposable Postgres database per test.

Constraints and upsert behaviour are the things most worth testing here, and
neither survives being mocked — so every test runs against a real database.
The suite creates one template database, applies the migrations to it once,
and then clones it per test, which is fast and leaves each test alone with its
own data. Everything created is dropped again at the end of the session.

The server is the one docker-compose publishes on loopback. Nothing here
reaches the network, and no API is called.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import psycopg
import pytest
from psycopg import conninfo as _conninfo

from dwb import migrate
from dwb.config import load_dotenv, resolve_dsn, resolve_password

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DB = f"dwb_test_template_{os.getpid()}"

load_dotenv(os.path.join(REPO_ROOT, ".env"))


def dsn_for(database):
    """Return the configured connection string, pointed at `database`."""
    parsed = _conninfo.conninfo_to_dict(resolve_dsn(os.environ.get("DWB_TEST_DSN")))
    parsed["dbname"] = database
    password = resolve_password()
    if password and not parsed.get("password"):
        parsed["password"] = password
    return _conninfo.make_conninfo(**parsed)


def _maintenance_connection():
    """Connect to the server's own database, for CREATE/DROP DATABASE."""
    try:
        return psycopg.connect(dsn_for("postgres"), autocommit=True, connect_timeout=10)
    except psycopg.OperationalError as e:
        raise pytest.UsageError(
            "The test suite needs the Order store running.\n"
            "Start it with:  docker compose up -d\n"
            f"Connection error: {e}")


def _drop_database(cur, name):
    cur.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture(scope="session")
def template_database():
    """A database with every migration applied, cloned by each test."""
    admin = _maintenance_connection()
    try:
        with admin.cursor() as cur:
            _drop_database(cur, TEMPLATE_DB)
            cur.execute(f'create database "{TEMPLATE_DB}"')
        with psycopg.connect(dsn_for(TEMPLATE_DB)) as conn:
            migrate.apply_pending(conn)
            conn.commit()
        yield TEMPLATE_DB
    finally:
        with admin.cursor() as cur:
            _drop_database(cur, TEMPLATE_DB)
        admin.close()


@pytest.fixture
def database(template_database, request):
    """A fresh database for one test; dropped when the test finishes."""
    name = f"dwb_test_{os.getpid()}_{abs(hash(request.node.nodeid)) % 10**8}"
    admin = _maintenance_connection()
    with admin.cursor() as cur:
        _drop_database(cur, name)
        cur.execute(f'create database "{name}" template "{template_database}"')
    try:
        yield dsn_for(name)
    finally:
        with admin.cursor() as cur:
            _drop_database(cur, name)
        admin.close()


@pytest.fixture
def conn(database):
    """An open connection to this test's database."""
    with psycopg.connect(database, autocommit=True) as connection:
        yield connection


@pytest.fixture
def empty_database():
    """A database with no migrations applied, for testing the runner itself."""
    name = f"dwb_test_empty_{os.getpid()}"
    admin = _maintenance_connection()
    with admin.cursor() as cur:
        _drop_database(cur, name)
        cur.execute(f'create database "{name}"')
    try:
        yield dsn_for(name)
    finally:
        with admin.cursor() as cur:
            _drop_database(cur, name)
        admin.close()


@pytest.fixture
def run_cli():
    """Run one of the repository's command-line entry points.

    Tests drive the real entry point in a subprocess rather than calling
    main() in-process, so argument parsing, exit codes and output are all
    covered — and so a test can never accidentally share state with the
    command it is testing.
    """

    def run(script, *args, **kwargs):
        env = dict(os.environ)
        env.update(kwargs.pop("env", {}))
        env.setdefault("PYTHONUNBUFFERED", "1")
        return subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, script), *args],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            check=False, timeout=kwargs.pop("timeout", 180), **kwargs)

    return run


def rows(conn, sql, params=None):
    """Run a query and return the rows, for readable assertions in tests."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def one(conn, sql, params=None):
    """Run a query expected to return a single row, and return it."""
    result = rows(conn, sql, params)
    assert len(result) == 1, f"expected 1 row, got {len(result)}"
    return result[0]


# ---------------------------------------------------------------------------
# Order fixtures
#
# Shaped like the payloads Digital Waybill actually returns: naive timestamps
# in three different formats, prices as strings, empty strings where a value
# is absent, and a First Stop carrying only Location fields.
# ---------------------------------------------------------------------------

FIRST_STOP = {
    "company": "UNITED AIRLINES CARGO FLL",
    "address": "1800 SW 34 STREET",
    "suite": "",
    "city": "FT. LAUDERDALE",
    "state": "FL",
    "postal_code": "33315",
    "country": "United States",
    "contact": {"name": "", "phone": "954-359-0829"},
}

FINAL_STOP = {
    "company": "LUFTHANSA TECHNIK",
    "address": "2650 SW 36 STREET",
    "suite": "2658",
    "city": "DANIA BEACH",
    "state": "FL",
    "postal_code": "33312",
    "country": "United States",
    "contact": {"name": "MATTHEW COHEN", "phone": "954-302-3922"},
    "paper_waybill": "",
    "special_instructions": "",
    "return_add": "0",
    "service_type": "VAN",
    "package": "STANDARD",
    "number_of_pieces": 1.0,
    "weight": 1.0,
    "packages": [{"package": "STANDARD", "number_of_pieces": 1.0, "weight": 1.0}],
    "vehicle": "",
    "driver_number": "ENGE",
    "dispatch_message": "UNITED AIRLINES CARGO FLL. To: LUFTHANSA TECHNIK. SRV: VAN. ",
    "notes": "",
    "signature_contact": "BRENT JERVEY ",
    "reference": "58225052846",
    "signature": "",
    "fuel_surcharge": "",
    "route_stop_id": 391806,
    "route_status": "4",
    "distance": "0",
    "air_distance": "0",
    "route_status_detail": "",
    "driver_pricelist": "LFTT 2022",
    "route_status_date": "Tue, 25 Nov 2025 11:04:17",
    "receive_date": "Tue, 25 Nov 2025 11:02:47",
    "dispatch_date": "Tue, 25 Nov 2025 11:02:59",
    "pickup_date": "Tue, 25 Nov 2025 11:03:37",
    "delivery_date": "Tue, 25 Nov 2025 11:04:17",
    "cancel_date": "Tue, 25 Nov 2025 11:02:47",
    "confirm_date": "Tue, 25 Nov 2025 11:02:47",
}


def make_order(order_number=401791, stops=None, **overrides):
    """Return an Order payload shaped like the API's."""
    # The dispatch system's own ids run a fixed distance below the Order
    # Number in the real data; deriving them keeps every fixture Order
    # distinct, which the uniqueness constraints care about. A test may pass a
    # deliberately unusable Order Number, which has no arithmetic.
    numeric = order_number if isinstance(order_number, int) else 0
    order = {
        "id": numeric - 10301,
        "time": "Tue, 25 Nov 2025 11:02:47",
        "order_number": order_number,
        "price": "60",
        "customer_number": "235",
        "cost_center": "LUFTHANSA TECHNIK",
        "final_price": 81.0,
        "flagged": 0,
        "read": False,
        "pending": False,
        "origin": "TelephoneOrder",
        "status_date": "11/25/2025 11:04:17 AM",
        "ready_time": "11/25/2025 11:02:00 AM",
        "deliver_by": "11/25/2025 3:02:00 PM",
        "status_detail": "",
        "dispatch_driver": "ENGE",
        "version": "2025-11-25 15:28:24.252",
        "comm_override": "",
        "recurring_name": "",
        "optimized_route": "",
        "status": "Completed",
        "order_type": "Pickup",
    }
    order.update(overrides)
    if stops is None:
        stops = [dict(FIRST_STOP),
                 dict(FINAL_STOP, route_stop_id=numeric - 9985)]
    order["route_stops"] = stops
    return order


def write_order(directory, order, filename=None):
    """Write an Order to a JSON file the way the fetcher does."""
    name = filename or "order_{}.json".format(order.get("order_number"))
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(order, fh, indent=2, ensure_ascii=False)
    return path


@pytest.fixture
def archive(tmp_path):
    """An empty directory standing in for the Order archive."""
    d = tmp_path / "orders"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# A stand-in for the Digital Waybill API
#
# Stubbed at the lowest point that still leaves something worth testing: a
# real HTTP server on loopback. Everything above it — Windows-1252 decoding,
# the {status, error, body} envelope, pagination, retries — stays under test,
# and the suite still makes no network calls and consumes no rate limit.
# ---------------------------------------------------------------------------

class FakeDispatch:
    """Serves Orders the way the API does, and records what was asked for."""

    def __init__(self):
        self.orders = {}          # order_number -> payload
        self.requests = []        # (path, query dict)
        self.page_size_cap = 50
        self._server = None
        self._thread = None

    # -- the Orders it holds ------------------------------------------------
    def add(self, *orders):
        for order in orders:
            self.orders[int(order["order_number"])] = order

    def newest_first(self):
        return [self.orders[n] for n in sorted(self.orders, reverse=True)]

    @property
    def order_requests(self):
        """Paths of single-Order requests — what a sweep costs."""
        return [p for p, _ in self.requests if p.count("/") > 2]

    @property
    def page_requests(self):
        return [q for p, q in self.requests if p.count("/") == 2]

    # -- running it ---------------------------------------------------------
    def start(self):
        dispatch = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                dispatch.requests.append((parsed.path, query))
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 3:
                    body = dispatch._single(parts[2])
                else:
                    body = dispatch._page(query)
                # The real API answers in Windows-1252, which is exactly why
                # the fetcher has its own decoding step.
                payload = json.dumps({"status": 200, "error": None, "body": body})
                encoded = payload.encode("cp1252", errors="replace")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=windows-1252")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self):
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    # -- responses ----------------------------------------------------------
    def _page(self, query):
        page_size = min(int(query.get("page_size", 50)), self.page_size_cap)
        page_num = int(query.get("page_num", 1))
        orders = self.newest_first()
        start = (page_num - 1) * page_size
        return {"count": len(orders), "page_size": page_size,
                "page_num": page_num, "orders": orders[start:start + page_size]}

    def _single(self, order_number):
        order = self.orders.get(int(order_number))
        return {"order": order} if order else {"orders": []}


@pytest.fixture
def dispatch():
    fake = FakeDispatch().start()
    try:
        yield fake
    finally:
        fake.stop()
