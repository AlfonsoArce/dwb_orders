"""What the Orders viewer answers over HTTP.

Driven through the API's own HTTP boundary against a real, disposable
database holding known Orders, because that is the seam worth testing: the
queries, the serialisation of money and timestamps, and the status codes are
all under it, and none of them survive being mocked.

Nothing here inspects generated SQL, row mapping, or response models. A test
reads as "given these Orders in the store, this is what the API returns".
"""

import psycopg
import pytest
from conftest import make_order
from fastapi.testclient import TestClient

from dwb import config, db
from dwb.ingest import ingest_orders
from dwb.viewer import create_app

# Nothing listens here, so connecting is refused immediately rather than
# waiting out a timeout.
UNREACHABLE_DSN = "postgresql://dwb@127.0.0.1:1/dwb_orders"


def viewer_for(dsn, assets=None):
    """A client for a viewer pointed wherever this test needs it."""
    return TestClient(create_app(dsn=dsn, assets=assets))


@pytest.fixture
def store(database):
    """Put Orders in this test's database, the way the fetcher does."""

    def add(*orders):
        with psycopg.connect(database) as conn:
            ingest_orders(conn, list(orders))
            conn.commit()

    return add


def health(client):
    response = client.get("/api/health")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The state of the store
# ---------------------------------------------------------------------------

def test_the_store_reports_itself_reachable(viewer):
    assert health(viewer)["database"] == {"reachable": True, "problem": None}


def test_the_newest_order_is_the_highest_order_number_and_when_it_was_placed(
    viewer, store
):
    store(
        make_order(401791, time="Tue, 25 Nov 2025 11:02:47"),
        make_order(401793, time="Tue, 25 Nov 2025 14:30:00"),
        make_order(401792, time="Tue, 25 Nov 2025 12:00:00"),
    )

    newest = health(viewer)["store"]["newest_order"]

    assert newest["order_number"] == 401793
    assert newest["placed_at"] == "2025-11-25T19:30:00+00:00"


def test_orders_without_a_terminal_status_are_counted_as_in_flight(viewer, store):
    store(
        make_order(401791, status="Completed"),
        make_order(401792, status="Cancelled"),
        make_order(401793, status="Dispatched"),
        make_order(401794, status="Confirmed"),
    )

    assert health(viewer)["store"]["in_flight_orders"] == 2


def test_an_order_with_no_status_at_all_is_counted_as_in_flight(viewer, store):
    """The Orders the sweep exists to chase — see migration 0005."""
    store(make_order(401791, status=""), make_order(401792, status="Completed"))

    assert health(viewer)["store"]["in_flight_orders"] == 1


def test_every_order_is_counted(viewer, store):
    store(make_order(401791), make_order(401792), make_order(401793))

    assert health(viewer)["store"]["total_orders"] == 3


def test_the_store_reports_when_it_last_wrote_an_order(viewer, store):
    store(make_order(401791))

    body = health(viewer)["store"]
    assert body["last_written_at"].endswith("+00:00")


def test_an_empty_store_reports_zero_rather_than_nothing(viewer):
    body = health(viewer)["store"]

    assert body["total_orders"] == 0
    assert body["in_flight_orders"] == 0
    assert body["newest_order"] is None
    assert body["last_written_at"] is None


def test_timestamps_cross_the_wire_as_utc(viewer, store):
    """The client formats for Miami; the wire carries no assumption of its own.

    The fixture Order was placed at 11:02:47 Eastern, which is 16:02:47 UTC.
    """
    store(make_order(401791, time="Tue, 25 Nov 2025 11:02:47"))

    assert (
        health(viewer)["store"]["newest_order"]["placed_at"]
        == "2025-11-25T16:02:47+00:00"
    )


# ---------------------------------------------------------------------------
# When the database is not there
# ---------------------------------------------------------------------------

def test_building_the_application_opens_no_connection(monkeypatch):
    """Nothing connects at import or startup, which is what lets the service
    stay up while the database is down — and what lets a test point it at a
    disposable database."""
    with viewer_for(UNREACHABLE_DSN):
        pass


def test_health_answers_successfully_when_the_database_is_unreachable():
    """A health check that goes down with its dependency tells you less than
    one that stays up and names the problem."""
    with viewer_for(UNREACHABLE_DSN) as client:
        response = client.get("/api/health")

        assert response.status_code == 200
        body = response.json()
        assert body["database"]["reachable"] is False
        assert "127.0.0.1" in body["database"]["problem"]
        assert body["store"] is None


def test_a_query_that_fails_after_connecting_is_reported_not_raised(empty_database):
    """The endpoint's job is to report, not to fail.

    `empty_database` has had no migrations applied, so there is no `orders`
    table to read: the connection succeeds and the query does not. That is the
    shape of a server going away mid-query, or of a table the viewer's role
    was never granted — neither should reach the reader as a 500.
    """
    with viewer_for(empty_database) as client:
        response = client.get("/api/health")

        assert response.status_code == 200
        body = response.json()
        assert body["database"]["reachable"] is False
        assert "orders" in body["database"]["problem"]
        assert body["store"] is None


def test_an_unreachable_database_never_reports_a_password():
    """Connection strings get logged and rendered; passwords must not be."""
    dsn = "postgresql://dwb:hunter2@127.0.0.1:1/dwb_orders"
    with viewer_for(dsn) as client:
        assert "hunter2" not in client.get("/api/health").text


# ---------------------------------------------------------------------------
# One origin
# ---------------------------------------------------------------------------

@pytest.fixture
def built_frontend(tmp_path):
    """Stand-in for frontend/dist, so the tests need no Node build."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<title>Orders</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("export {}", encoding="utf-8")
    return dist


def test_the_frontend_is_served_from_the_same_origin_as_the_api(
    database, built_frontend
):
    with viewer_for(database, assets=built_frontend) as client:
        assert "<title>Orders</title>" in client.get("/").text
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/api/health").status_code == 200


def test_a_client_side_route_falls_back_to_the_frontend(database, built_frontend):
    """The viewer's screens are paths, so a reload of one must not 404."""
    with viewer_for(database, assets=built_frontend) as client:
        assert "<title>Orders</title>" in client.get("/a-client-side-route").text


def test_an_unknown_api_path_is_not_answered_with_the_frontend(
    database, built_frontend
):
    with viewer_for(database, assets=built_frontend) as client:
        assert client.get("/api/nothing-here").status_code == 404


def test_the_api_answers_without_a_built_frontend(database, tmp_path):
    """Development runs the frontend from Vite, so dist need not exist."""
    with viewer_for(database, assets=str(tmp_path / "never-built")) as client:
        assert client.get("/api/health").status_code == 200
        assert "npm run build" in client.get("/").text


# ---------------------------------------------------------------------------
# Whose password the viewer uses
# ---------------------------------------------------------------------------

def test_the_viewer_never_borrows_the_admin_password(monkeypatch):
    """A read-only account authenticating with the owning account's secret is
    not a second account at all, so the fallback deliberately does not exist."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "the-admin-password")
    monkeypatch.delenv("DWB_VIEWER_PASSWORD", raising=False)

    assert config.resolve_viewer_password() is None


def test_the_viewer_uses_its_own_password_when_one_is_set(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "the-admin-password")
    monkeypatch.setenv("DWB_VIEWER_PASSWORD", "the-viewers-password")

    assert config.resolve_viewer_password() == "the-viewers-password"


def test_the_viewer_has_its_own_connection_string(monkeypatch):
    """A different user and, inside compose, a different address."""
    monkeypatch.setenv("DWB_DSN", "postgresql://dwb@127.0.0.1:5434/dwb_orders")
    monkeypatch.setenv("DWB_VIEWER_DSN", "postgresql://dwb_viewer@db:5432/dwb_orders")

    assert config.resolve_viewer_dsn() == "postgresql://dwb_viewer@db:5432/dwb_orders"


def test_connecting_with_no_password_does_not_reach_for_the_admin_one(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "the-admin-password")
    attempted = {}

    def record(dsn, **kwargs):
        attempted.update(kwargs)
        raise psycopg.OperationalError("not connecting, thanks")

    monkeypatch.setattr(psycopg, "connect", record)
    with pytest.raises(db.DatabaseUnavailable):
        db.connect(UNREACHABLE_DSN, password=None)

    assert "password" not in attempted
