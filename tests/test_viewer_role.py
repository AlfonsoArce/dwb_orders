"""The read-only role the Orders viewer connects as.

Read-only is the viewer's central safety claim, and it is claimed of Postgres
rather than of the application code above it — so it is tested here, against
the database, by trying to write and being refused.

Privileges are exercised with `set role` rather than by opening a second
connection. The role is deliberately created without a password (a migration
is committed, so it cannot carry one), which means it cannot log in until
somebody sets one by hand; `set role` reaches the same access-control rules
without needing that step, and without a test mutating a cluster-level
password the running viewer is using.

This assumes the connecting user may assume the role, which the bootstrap
superuser docker-compose creates can.
"""

import os

import psycopg
import pytest
from conftest import one

from dwb import migrate

VIEWER = "dwb_viewer"
MIGRATION = "0006_read_only_viewer_role.sql"

# Every table the store has, plus the runner's own bookkeeping: the viewer is
# granted the schema, not a hand-picked list, so all of them must be readable.
TABLES = ["sources", "orders", "route_stops", "import_progress", "schema_migrations"]

WRITES = [
    ("insert", "insert into sources (source_key, name) values ('x', 'X')"),
    ("update", "update sources set name = 'X'"),
    ("delete", "delete from sources"),
]


def add_order(conn, order_number=401791):
    return one(
        conn,
        "insert into orders (source_key, order_number, raw) "
        "values ('digital_waybill', %s, '{}'::jsonb) returning order_id",
        (order_number,),
    )[0]


def test_the_role_exists_and_can_log_in(conn):
    assert one(
        conn, "select rolcanlogin from pg_roles where rolname = %s", (VIEWER,)
    ) == (True,)


def test_the_migration_carries_no_password():
    """A committed migration must never carry a secret.

    Asserted against the file, not against the role's stored password. The role
    is cluster-level and the documented setup step sets a password on it by
    hand, so a test asserting that column is null would pass until somebody
    followed the instructions and then fail forever — on every machine where
    the viewer actually works. What is worth pinning here is the file.
    """
    path = os.path.join(migrate.MIGRATIONS_DIR, MIGRATION)
    with open(path, encoding="utf-8") as fh:
        statements = [line for line in fh if not line.lstrip().startswith("--")]

    assert not [line for line in statements if "password" in line.lower()]


@pytest.mark.parametrize("table", TABLES)
def test_the_role_can_read_every_table(conn, table):
    add_order(conn)

    with conn.cursor() as cur:
        cur.execute(f"set role {VIEWER}")
        cur.execute(f"select count(*) from {table}")  # refused -> raises
        cur.execute("reset role")


@pytest.mark.parametrize("verb, sql", WRITES, ids=[verb for verb, _ in WRITES])
def test_the_role_is_refused_every_write(conn, verb, sql):
    with conn.cursor() as cur:
        cur.execute(f"set role {VIEWER}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(sql)


def test_the_role_cannot_create_a_table_of_its_own(conn):
    with conn.cursor() as cur:
        cur.execute(f"set role {VIEWER}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("create table sneaky (id int)")


def test_a_table_a_later_migration_adds_is_readable_without_a_further_grant(conn):
    """The grant that matters most, because forgetting it is silent.

    A future migration creating a table must not leave the viewer unable to
    read it until somebody remembers. Default privileges cover that; this
    stands in for migration 0007.
    """
    with conn.cursor() as cur:
        cur.execute("create table later_migration_table (id int)")
        cur.execute(f"set role {VIEWER}")
        cur.execute("select count(*) from later_migration_table")
        cur.execute("reset role")


def test_the_role_still_cannot_write_a_table_a_later_migration_adds(conn):
    with conn.cursor() as cur:
        cur.execute("create table later_migration_table (id int)")
        cur.execute(f"set role {VIEWER}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("insert into later_migration_table values (1)")


def test_the_migrations_apply_to_a_second_database_in_the_same_cluster(
    template_database, empty_database
):
    """The trap this migration is shaped around.

    A role is a cluster-level object; migrations are applied per database. The
    template database has already had them applied, so the role exists — and a
    plain `create role` here would fail. Depending on the template fixture is
    what makes that ordering certain rather than incidental.
    """
    with psycopg.connect(empty_database) as conn:
        migrate.apply_pending(conn)
        conn.commit()

        assert one(
            conn, "select count(*) from pg_roles where rolname = %s", (VIEWER,)
        ) == (1,)
        # And the grants landed in this database too, not just the first one.
        assert one(
            conn, "select has_table_privilege(%s, 'orders', 'select')", (VIEWER,)
        ) == (True,)
        assert one(
            conn, "select has_table_privilege(%s, 'orders', 'insert')", (VIEWER,)
        ) == (False,)
