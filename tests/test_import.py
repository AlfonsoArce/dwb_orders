"""Importing the archive: batching, resuming, and honest accounting."""

import os

import psycopg
import pytest
from conftest import make_order, one, rows, write_order


@pytest.fixture
def db(database):
    with psycopg.connect(database, autocommit=True) as conn:
        yield conn


@pytest.fixture
def load(run_cli, database, archive):
    def run(*args, expect=0):
        result = run_cli("import_orders.py", "--input-dir", str(archive),
                         "--dsn", database, "--no-progress", *args)
        assert result.returncode == expect, result.stdout + result.stderr
        return result

    return run


def fill(archive, count, start=401791):
    for number in range(start, start + count):
        write_order(archive, make_order(order_number=number))


def test_every_order_file_in_the_directory_is_loaded(archive, load, db):
    fill(archive, 10)

    result = load()

    assert one(db, "select count(*) from orders") == (10,)
    assert one(db, "select count(*) from route_stops") == (20,)
    assert "Orders loaded:      10" in result.stdout
    assert "Route Stops loaded: 20" in result.stdout
    assert "Files not read:     0" in result.stdout


def test_progress_is_recorded_so_a_later_run_can_resume(archive, load, db):
    fill(archive, 6)

    load("--batch-size", "2")

    last_file, files_done = one(
        db, "select last_file, files_done from import_progress")
    assert last_file == "order_401796.json"
    assert files_done == 6


def test_an_interrupted_run_resumes_without_reprocessing_everything(archive, load, db):
    fill(archive, 6)

    load("--batch-size", "2", "--limit", "4")
    assert one(db, "select count(*) from orders") == (4,)

    result = load()

    assert "Files already done: 4 (resumed)" in result.stdout
    assert "Files read:         2" in result.stdout
    assert one(db, "select count(*) from orders") == (6,)


def test_rerunning_a_completed_import_changes_nothing(archive, load, db):
    fill(archive, 4)
    load()
    before = rows(db, "select order_number, updated_at from orders order by order_number")

    resumed = load()
    assert "Files already done: 4" in resumed.stdout
    assert "Orders loaded:      0" in resumed.stdout

    reread = load("--restart")
    assert "Files read:         4" in reread.stdout
    assert "Orders loaded:      0" in reread.stdout

    after = rows(db, "select order_number, updated_at from orders order by order_number")
    assert after == before


def test_damaged_files_are_counted_separately_and_not_skipped(archive, load, db):
    fill(archive, 2)
    (archive / "order_500001.json").write_text("")                    # empty
    (archive / "order_500002.json").write_text("{not json")           # unreadable
    (archive / "order_500003.json").write_text('{"status": "New"}')   # no Order Number
    os.symlink(str(archive / "gone.json"), str(archive / "order_500004.json"))  # vanished

    result = load()

    assert "Files found:        6" in result.stdout
    assert "Files not read:     4" in result.stdout
    assert "  vanished:       1" in result.stdout
    assert "  empty:          1" in result.stdout
    assert "  unreadable:     1" in result.stdout
    assert "  unusable:       1" in result.stdout
    assert one(db, "select count(*) from orders") == (2,)


def test_sync_conflict_copies_are_matched_rather_than_ignored(archive, load, db):
    write_order(archive, make_order(order_number=401791, status="Confirmed"))
    write_order(archive, make_order(order_number=401791, status="Completed",
                                    version="2025-11-26 09:00:00.000"),
                filename="order_401791 2.json")

    result = load()

    assert "Files found:        2" in result.stdout
    assert "Files not read:     0" in result.stdout
    # One Order: the conflict copy is a later revision of the same Order, and
    # the revision guard picks the newer of the two.
    assert one(db, "select count(*) from orders") == (1,)
    assert one(db, "select order_status from orders") == ("Completed",)
    assert one(db, "select count(*) from route_stops") == (2,)


def test_a_missing_input_directory_is_reported_rather_than_crashing(
        run_cli, database, tmp_path):
    result = run_cli("import_orders.py", "--input-dir", str(tmp_path / "nope"),
                     "--dsn", database, "--no-progress")

    assert result.returncode == 2
    assert "No such directory" in result.stderr
