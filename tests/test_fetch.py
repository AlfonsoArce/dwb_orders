"""The fetcher, driven from the command line with the API stubbed on loopback.

Nothing here reaches the network or consumes rate limit, and every assertion
is about what ended up in the database.
"""

import datetime as dt
import os
from zoneinfo import ZoneInfo

import psycopg
import pytest
from conftest import make_order, one, rows

from dwb.coerce import EASTERN


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


# --- 10: holes below the watermark -----------------------------------------
#
# An incremental run resumes from the highest Order Number stored and stops at
# the first Order it already has. That is only safe while the stored Orders
# reach down to meet the ones below them. A run that stops early — the page
# ceiling, or an API that stopped answering — stores a block that does not,
# and every later run reads a watermark above the hole. These lock in that the
# hole is reported when it is made, and reachable afterwards.

def _api_time(days_ago):
    """A placed-at the way the API writes it, relative to now.

    Naive and Eastern, because that is what the API sends and what the store
    assumes (ADR-0001) — a UTC wall clock here would put an Order on the wrong
    side of the window for five hours of every day.
    """
    now = dt.datetime.now(ZoneInfo(EASTERN))
    return (now - dt.timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S")


def test_a_truncated_incremental_run_reports_the_hole_it_left(fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401801)])
    fetch()

    # Twenty more Orders arrive, and the next run is allowed one page of five.
    dispatch.add(*[make_order(n) for n in range(401801, 401821)])
    result = fetch("--incremental", "--page-size", "5", "--max-pages", "1",
                   expect=3)

    # It kept the newest five and never asked for 401801-401815, which now sit
    # below a watermark of 401820. Exiting 0 here is the bug: the run looked
    # like a success and had just made 15 Orders unreachable.
    assert one(db, "select max(order_number) from orders") == (401820,)
    assert one(db, "select count(*) from orders "
                   "where order_number between 401801 and 401815") == (0,)
    assert "401801" in result.stderr and "401815" in result.stderr


def test_a_run_that_meets_the_stored_orders_reports_no_hole(fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401801)])
    fetch()

    dispatch.add(*[make_order(n) for n in range(401801, 401806)])
    # Room enough to page down to what is already stored, so nothing is skipped.
    fetch("--incremental", "--page-size", "5", "--max-pages", "10")

    assert one(db, "select count(*) from orders") == (15,)


def test_paging_can_never_recover_the_hole_on_its_own(fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401801)])
    fetch()
    dispatch.add(*[make_order(n) for n in range(401801, 401821)])
    fetch("--incremental", "--page-size", "5", "--max-pages", "1", expect=3)

    # Unlimited pages, and it still cannot help: the missing Orders are below
    # the watermark, so the first page reaches known territory and stops. The
    # overlap window is the only thing that ever looks below the watermark,
    # and a hole wider than the overlap is beyond it.
    fetch("--incremental", "--max-pages", "100", "--overlap-pages", "0")

    assert one(db, "select count(*) from orders "
                   "where order_number between 401801 and 401815") == (0,)


def test_filling_the_gaps_collects_what_paging_cannot_reach(fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401801)])
    fetch()
    dispatch.add(*[make_order(n) for n in range(401801, 401821)])
    fetch("--incremental", "--page-size", "5", "--max-pages", "1", expect=3)

    fetch("--incremental", "--overlap-pages", "0", "--fill-gaps", "--fill-all")

    assert one(db, "select count(*) from orders") == (30,)
    assert rows(db, "select order_number from orders "
                    "where order_number between 401801 and 401815 "
                    "order by order_number") == [(n,) for n in range(401801, 401816)]


def test_a_run_that_fills_its_own_gap_reports_success(fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401801)])
    fetch()
    dispatch.add(*[make_order(n) for n in range(401801, 401821)])

    # Truncated, so it leaves a hole — and collects it in the same run, which
    # is what the poller does. Nothing is left outstanding, so exit 0.
    fetch("--incremental", "--page-size", "5", "--max-pages", "1",
          "--fill-gaps", "--fill-all")

    assert one(db, "select count(*) from orders") == (30,)


def test_order_numbers_that_never_existed_are_not_an_error(fetch, dispatch, db):
    # The numbering has always had holes. Asking about one answers 404, which
    # is an answer, not a failure: the run carries on and exits 0.
    dispatch.add(*[make_order(n) for n in range(401791, 401796)])
    dispatch.add(*[make_order(n) for n in range(401801, 401806)])
    fetch()

    result = fetch("--incremental", "--overlap-pages", "0", "--fill-gaps",
                   "--fill-all")

    assert one(db, "select count(*) from orders") == (10,)
    assert len(dispatch.order_requests) == 5      # 401796-401800, all absent
    assert "5 Order Number(s) never existed" in result.stdout


def test_the_fill_window_bounds_how_far_back_it_looks(fetch, dispatch, db):
    old, recent = _api_time(400), _api_time(2)
    dispatch.add(*[make_order(n, time=old)
                   for n in list(range(401791, 401796)) + list(range(401801, 401806))])
    dispatch.add(*[make_order(n, time=recent)
                   for n in list(range(401811, 401816)) + list(range(401821, 401826))])
    fetch()

    fetch("--incremental", "--overlap-pages", "0", "--fill-gaps", "--fill-days", "30")

    # Only the hole among Orders placed inside the window was asked about; the
    # one among Orders placed over a year ago was left alone.
    assert sorted(dispatch.order_requests) == [
        f"/TESTCO/orders.json/{n}" for n in range(401816, 401821)]


def test_the_whole_range_is_searched_when_the_window_is_waived(fetch, dispatch, db):
    old, recent = _api_time(400), _api_time(2)
    dispatch.add(*[make_order(n, time=old)
                   for n in list(range(401791, 401796)) + list(range(401801, 401806))])
    dispatch.add(*[make_order(n, time=recent)
                   for n in list(range(401811, 401816)) + list(range(401821, 401826))])
    fetch()

    fetch("--incremental", "--overlap-pages", "0", "--fill-gaps", "--fill-all")

    assert len(dispatch.order_requests) == 15     # 401796-401800, 401806-401810,
                                                  # 401816-401820


def test_the_fill_is_bounded(fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401796)])
    dispatch.add(*[make_order(n) for n in range(401811, 401816)])
    fetch()

    fetch("--incremental", "--overlap-pages", "0", "--fill-gaps", "--fill-all",
          "--fill-limit", "4")

    assert len(dispatch.order_requests) == 4


def test_the_fill_preview_names_the_gaps_and_requests_none_of_them(
        fetch, dispatch, db):
    dispatch.add(*[make_order(n) for n in range(401791, 401796)])
    dispatch.add(*[make_order(n) for n in range(401801, 401806)])
    fetch()

    result = fetch("--incremental", "--overlap-pages", "0", "--fill-preview",
                   "--fill-all", "--console-level", "INFO")

    assert "#401800" in result.stderr
    assert dispatch.order_requests == []
