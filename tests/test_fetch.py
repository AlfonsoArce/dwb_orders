"""The fetcher, driven from the command line with the API stubbed on loopback.

Nothing here reaches the network or consumes rate limit, and every assertion
is about what ended up in the database.
"""

import os

import psycopg
import pytest
from conftest import make_order, one, rows


@pytest.fixture
def db(database):
    with psycopg.connect(database, autocommit=True) as conn:
        yield conn


@pytest.fixture
def fetch(run_cli, database, dispatch, archive):
    """Run the fetcher against the stub API and this test's database."""

    def run(*args, expect=0, dsn=None, **env):
        result = run_cli(
            "get_orders.py", "--cid", "TESTCO", "--key", "test-key",
            "--dsn", database if dsn is None else dsn,
            "--out-dir", str(archive), "--no-excel",
            "--max-pages", "10", "--page-delay", "0", "--console-level", "ERROR",
            *args,
            env=dict({"DWB_BASE_URL": dispatch.url}, **env))
        assert result.returncode == expect, result.stdout + result.stderr
        return result

    return run


# --- 07: the fetcher writes Orders to the database -------------------------

def test_every_order_retrieved_is_stored(fetch, dispatch, db):
    dispatch.add(make_order(401791), make_order(401792))

    fetch()

    assert rows(db, "select order_number from orders order by order_number") \
        == [(401791,), (401792,)]
    assert one(db, "select count(*) from route_stops") == (4,)


def test_json_files_are_still_written_by_default(fetch, dispatch, archive):
    dispatch.add(make_order(401791))

    fetch()

    assert os.path.exists(str(archive / "order_401791.json"))


def test_a_flag_stops_the_json_files_being_written(fetch, dispatch, archive, db):
    dispatch.add(make_order(401791))

    fetch("--no-json")

    assert os.listdir(str(archive)) == []
    assert one(db, "select count(*) from orders") == (1,)


def test_the_dsn_flag_beats_the_environment(fetch, dispatch, db, database):
    dispatch.add(make_order(401791))

    fetch(DWB_DSN="postgresql://nobody@127.0.0.1:5999/wrong")

    assert one(db, "select count(*) from orders") == (1,)


def test_the_dsn_can_come_from_the_environment(run_cli, dispatch, db, database, archive):
    dispatch.add(make_order(401791))

    result = run_cli("get_orders.py", "--cid", "TESTCO", "--key", "test-key",
                     "--out-dir", str(archive), "--no-excel", "--max-pages", "10",
                     "--page-delay", "0", "--console-level", "ERROR",
                     env={"DWB_BASE_URL": dispatch.url, "DWB_DSN": database})

    assert result.returncode == 0, result.stdout + result.stderr
    assert one(db, "select count(*) from orders") == (1,)


def test_an_unreachable_database_fails_before_a_single_request(fetch, dispatch):
    dispatch.add(make_order(401791))

    result = fetch(dsn="postgresql://nobody@127.0.0.1:5999/wrong", expect=2)

    assert "docker compose up -d" in result.stderr
    assert dispatch.requests == []


def test_refetching_the_same_orders_changes_nothing(fetch, dispatch, db):
    dispatch.add(make_order(401791))
    fetch()
    before = rows(db, "select order_number, updated_at from orders")

    fetch()

    assert rows(db, "select order_number, updated_at from orders") == before


def test_no_credentials_appear_in_the_output(fetch, dispatch, database):
    dispatch.add(make_order(401791))

    result = fetch("--log-level", "DEBUG", "--console-level", "DEBUG",
                   "--password", "hunter2")

    output = result.stdout + result.stderr
    assert "test-key" not in output
    assert "hunter2" not in output


def test_an_order_without_a_usable_number_does_not_lose_the_page(
        fetch, dispatch, db):
    dispatch.add(make_order(401791))
    dispatch.orders[0] = make_order("")  # keyed at 0 so it rides along on the page

    fetch()

    assert rows(db, "select order_number from orders") == [(401791,)]


# --- 08: the watermark comes from SQL, and recent pages are re-read --------

def test_an_empty_database_causes_a_full_fetch(fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401801)])

    fetch("--incremental", "--page-size", "5")

    assert one(db, "select count(*) from orders") == (10,)


def test_an_incremental_run_resumes_from_the_database_not_the_folder(
        fetch, dispatch, db, archive):
    dispatch.add(*[make_order(n) for n in range(401791, 401801)])
    fetch()
    # Whatever the folder says is irrelevant: the watermark is a query.
    for name in os.listdir(str(archive)):
        os.remove(os.path.join(str(archive), name))
    dispatch.add(make_order(401801))
    already_requested = len(dispatch.page_requests)

    fetch("--incremental", "--page-size", "5", "--overlap-pages", "0")

    assert one(db, "select count(*) from orders") == (11,)
    # The newest page already reached the watermark, so nothing else was asked
    # for — and no directory listing was needed to work that out.
    pages = [q["page_num"] for q in dispatch.page_requests[already_requested:]]
    assert pages == ["1"]


def test_orders_that_have_not_changed_produce_no_writes(fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401801)])
    fetch()
    before = rows(db, "select order_number, updated_at from orders order by order_number")

    fetch("--incremental")

    assert rows(db, "select order_number, updated_at from orders order by order_number") \
        == before


def test_an_order_that_moved_to_a_terminal_status_is_updated_by_the_overlap(
        fetch, dispatch, db):
    dispatch.add(make_order(401791, status="Confirmed"))
    dispatch.add(*[make_order(n) for n in range(401792, 401800)])
    fetch()
    assert one(db, "select order_status from orders where order_number = 401791") \
        == ("Confirmed",)

    # Dispatch completes the Order. It is below the watermark now, so only the
    # overlap can bring it back.
    dispatch.add(make_order(401791, status="Completed",
                            version="2025-12-01 09:00:00.000"))
    fetch("--incremental", "--page-size", "3", "--overlap-pages", "3")

    assert one(db, "select order_status, is_terminal from orders "
                   "where order_number = 401791") == ("Completed", True)


def test_without_the_overlap_the_watermark_hides_the_change(fetch, dispatch, db):
    dispatch.add(make_order(401791, status="Confirmed"))
    dispatch.add(*[make_order(n) for n in range(401792, 401800)])
    fetch()

    dispatch.add(make_order(401791, status="Completed",
                            version="2025-12-01 09:00:00.000"))
    fetch("--incremental", "--page-size", "3", "--overlap-pages", "0")

    assert one(db, "select order_status from orders where order_number = 401791") \
        == ("Confirmed",)


# --- 09: sweeping the Orders still In Flight -------------------------------

def test_no_orders_are_requested_individually_unless_the_sweep_is_asked_for(
        fetch, dispatch, db):
    dispatch.add(make_order(401791, status="Confirmed"))

    fetch()

    assert dispatch.order_requests == []


def test_the_sweep_resolves_a_stalled_order(fetch, dispatch, db):
    dispatch.add(make_order(401791, status="Confirmed"))
    dispatch.add(*[make_order(n) for n in range(401792, 401800)])
    fetch()

    dispatch.add(make_order(401791, status="Completed",
                            version="2025-12-01 09:00:00.000"))
    fetch("--incremental", "--overlap-pages", "0", "--refresh-in-flight")

    assert one(db, "select order_status from orders where order_number = 401791") \
        == ("Completed",)
    assert dispatch.order_requests == ["/TESTCO/orders.json/401791"]


def test_the_sweep_only_asks_about_orders_that_are_in_flight(fetch, dispatch, db):
    dispatch.add(make_order(401791, status="Confirmed"),
                 make_order(401792, status="Completed"),
                 make_order(401793, status="PickedUp"))
    fetch()

    fetch("--refresh-in-flight", "--overlap-pages", "0", "--incremental")

    assert sorted(dispatch.order_requests) == ["/TESTCO/orders.json/401791",
                                               "/TESTCO/orders.json/401793"]


def test_the_sweep_is_bounded(fetch, dispatch, db):
    dispatch.add(*[make_order(n, status="Confirmed") for n in range(401791, 401801)])
    fetch()

    fetch("--refresh-in-flight", "--sweep-limit", "3", "--overlap-pages", "0",
          "--incremental")

    assert len(dispatch.order_requests) == 3


def test_preview_reports_what_it_would_request_and_requests_nothing(
        fetch, dispatch, db):
    dispatch.add(make_order(401791, status="Confirmed"),
                 make_order(401792, status="Completed"))
    fetch()

    result = fetch("--sweep-preview", "--overlap-pages", "0", "--incremental",
                   "--console-level", "INFO")

    assert "#401791" in result.stderr
    assert dispatch.order_requests == []


def test_the_sweep_writes_nothing_when_nothing_changed(fetch, dispatch, db):
    dispatch.add(make_order(401791, status="Confirmed"))
    fetch()
    before = rows(db, "select order_number, updated_at from orders")

    fetch("--refresh-in-flight", "--overlap-pages", "0", "--incremental")

    assert dispatch.order_requests == ["/TESTCO/orders.json/401791"]
    assert rows(db, "select order_number, updated_at from orders") == before
