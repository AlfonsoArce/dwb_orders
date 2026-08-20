"""Verification: proving the database is a faithful copy, or saying why not."""

import os

import psycopg
import pytest
from conftest import make_order, rows, write_order


@pytest.fixture
def db(database):
    with psycopg.connect(database, autocommit=True) as conn:
        yield conn


@pytest.fixture
def load(run_cli, database, archive):
    def run(*args):
        result = run_cli("import_orders.py", "--input-dir", str(archive),
                         "--dsn", database, "--no-progress", *args)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    return run


@pytest.fixture
def check(run_cli, database, archive):
    def run(*args, expect=0):
        result = run_cli("verify_import.py", "--input-dir", str(archive),
                         "--dsn", database, "--no-progress", *args)
        assert result.returncode == expect, result.stdout + result.stderr
        return result

    return run


def fill(archive, count, start=401791):
    for number in range(start, start + count):
        write_order(archive, make_order(order_number=number))


def test_a_faithful_import_verifies(archive, load, check):
    fill(archive, 10)
    load()

    result = check()

    assert "VERIFIED" in result.stdout
    assert "Orders   in archive:        10   stored:        10" in result.stdout
    assert "Stops    in archive:        20   stored:        20" in result.stdout


def test_the_sample_size_is_configurable_and_reported(archive, load, check):
    fill(archive, 10)
    load()

    result = check("--sample", "4")

    assert "Orders sampled and diffed against their file: 4 (asked for 4)" in result.stdout


def test_an_order_missing_from_the_database_fails_the_check(archive, load, check, db):
    fill(archive, 10)
    load()
    with db.cursor() as cur:
        cur.execute("delete from orders where order_number = 401795")

    result = check(expect=1)

    assert "MISSING from the database: 1" in result.stdout
    assert "#401795" in result.stdout
    assert "FAILED" in result.stdout


def test_a_payload_that_no_longer_matches_its_file_fails_the_check(
        archive, load, check, db):
    fill(archive, 3)
    load()
    with db.cursor() as cur:
        cur.execute("""update orders set raw = jsonb_set(raw, '{cost_center}',
                       '"SOMETHING ELSE"') where order_number = 401792""")

    result = check("--sample", "3", expect=1)

    assert "PAYLOAD differs for #401792" in result.stdout
    assert "cost_center" in result.stdout


def test_a_stop_that_went_missing_fails_the_check(archive, load, check, db):
    fill(archive, 3)
    load()
    with db.cursor() as cur:
        cur.execute("""delete from route_stops where order_id =
                       (select order_id from orders where order_number = 401792)
                       and stop_position = 2""")

    result = check(expect=1)

    assert "STOP COUNT differs for #401792: file 2, database 1" in result.stdout


def test_a_damaged_file_fails_the_check_rather_than_being_ignored(
        archive, load, check):
    fill(archive, 3)
    load()
    (archive / "order_500001.json").write_text("{not json")

    result = check(expect=1)

    assert "unreadable:     1" in result.stdout


def test_verification_changes_nothing(archive, load, check, db):
    fill(archive, 5)
    load()
    before = rows(db, "select order_number, updated_at, raw from orders order by 1")

    check()

    assert rows(db, "select order_number, updated_at, raw from orders order by 1") == before


def test_the_same_seed_samples_the_same_orders(archive, load, check):
    fill(archive, 20)
    load()

    first = check("--sample", "5", "--seed", "7")
    second = check("--sample", "5", "--seed", "7")

    assert first.stdout == second.stdout


# --- retiring the archive, once it is safe to ------------------------------

@pytest.fixture
def backups(tmp_path):
    """A backup directory, empty until a test says a dump was taken."""
    d = tmp_path / "backups"
    d.mkdir()
    return d


@pytest.fixture
def retire(run_cli, database, archive, backups, tmp_path):
    def run(*args, expect=0, destination=None):
        target = str(destination or (tmp_path / "cold" / "orders"))
        result = run_cli("verify_import.py", "--input-dir", str(archive),
                         "--dsn", database, "--no-progress",
                         "--backup-dir", str(backups), "--retire-to", target, *args)
        assert result.returncode == expect, result.stdout + result.stderr
        return result

    return run


def test_retiring_the_archive_is_described_before_it_is_done(
        archive, load, retire, backups, tmp_path):
    fill(archive, 3)
    load()
    (backups / "dwb_orders_20260820T000000.dump").write_bytes(b"dump")

    result = retire()

    assert "Nothing moved. Add --yes to do it." in result.stdout
    assert len(os.listdir(str(archive))) == 3


def test_the_archive_is_moved_not_deleted(archive, load, retire, backups, tmp_path):
    fill(archive, 3)
    load()
    (backups / "dwb_orders_20260820T000000.dump").write_bytes(b"dump")

    result = retire("--yes")

    assert "Moved." in result.stdout
    cold = tmp_path / "cold" / "orders"
    assert sorted(p.name for p in cold.iterdir()) == [
        "order_401791.json", "order_401792.json", "order_401793.json"]
    # The folder itself stays: the fetcher and the exporters still expect it.
    assert os.listdir(str(archive)) == [".gitkeep"]


def test_the_archive_is_not_moved_without_a_database_dump(
        archive, load, retire, tmp_path):
    fill(archive, 3)
    load()

    result = retire("--yes", expect=1)

    assert "No database dump found" in result.stderr
    assert len(os.listdir(str(archive))) == 3


def test_the_archive_is_not_moved_when_verification_fails(
        archive, load, retire, backups, db):
    fill(archive, 3)
    load()
    (backups / "dwb_orders_20260820T000000.dump").write_bytes(b"dump")
    with db.cursor() as cur:
        cur.execute("delete from orders where order_number = 401792")

    result = retire("--yes", expect=1)

    assert "Verification failed" in result.stderr
    assert len(os.listdir(str(archive))) == 3


def test_the_archive_is_not_moved_into_another_synced_folder(
        archive, load, retire, backups, tmp_path):
    fill(archive, 3)
    load()
    (backups / "dwb_orders_20260820T000000.dump").write_bytes(b"dump")

    result = retire("--yes", expect=1,
                    destination=str(tmp_path / "Mobile Documents" / "orders"))

    assert "inside a synced folder" in result.stderr
    assert len(os.listdir(str(archive))) == 3
