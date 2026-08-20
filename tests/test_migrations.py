"""The migration runner, driven from the command line."""

import psycopg
from conftest import one, rows

from dwb import migrate

ALL_VERSIONS = [(version,) for version, _ in migrate.discover()]


def test_applies_pending_migrations_and_records_them(run_cli, empty_database):
    result = run_cli("migrate.py", "--dsn", empty_database)

    assert result.returncode == 0, result.stderr
    assert "0001_sources.sql" in result.stdout

    with psycopg.connect(empty_database) as conn:
        applied = rows(conn, "select version from schema_migrations order by version")
        assert applied == ALL_VERSIONS
        assert one(conn, "select name from sources where source_key = 'digital_waybill'") \
            == ("Digital Waybill",)


def test_rerunning_with_nothing_pending_changes_nothing(run_cli, empty_database):
    run_cli("migrate.py", "--dsn", empty_database)

    with psycopg.connect(empty_database) as conn:
        before = rows(conn, "select version, applied_at from schema_migrations order by version")

    result = run_cli("migrate.py", "--dsn", empty_database)
    assert result.returncode == 0, result.stderr
    assert "Nothing to apply" in result.stdout

    with psycopg.connect(empty_database) as conn:
        after = rows(conn, "select version, applied_at from schema_migrations order by version")
    assert after == before


def test_reports_a_clear_error_when_the_database_is_unreachable(run_cli):
    result = run_cli("migrate.py", "--dsn",
                     "postgresql://nobody@127.0.0.1:5999/nothing_here")

    assert result.returncode == 2
    assert "docker compose up -d" in result.stderr


def test_a_migrated_database_has_the_migrations_applied(conn):
    assert rows(conn, "select version from schema_migrations order by version") == ALL_VERSIONS
