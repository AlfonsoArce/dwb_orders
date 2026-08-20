"""Apply numbered SQL migrations, once each, in order.

Schema changes are plain .sql files in migrations/, named NNNN_slug.sql. This
runner records what it has applied in schema_migrations, so running it again
with nothing pending does nothing, and adding a column later never means
re-importing 169,000 Orders.

Deliberately not wired into the container's initialisation scripts: those run
only when the data directory is empty, which would tie every schema change to
a rebuild of the database.
"""

import argparse
import hashlib
import os
import re
import sys

from dwb import db
from dwb.config import load_dotenv

# Session-level advisory lock, so two runners cannot apply the same migration
# concurrently. The number is arbitrary and means nothing beyond "this app".
LOCK_KEY = 8_147_063_301

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")

_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

TRACKING_TABLE_SQL = """
create table if not exists schema_migrations (
    version     text        primary key,
    filename    text        not null,
    checksum    text        not null,
    applied_at  timestamptz not null default now()
)
"""


class MigrationError(Exception):
    """A migration cannot be applied, or an applied one has since changed."""


def discover(directory=None):
    """Return [(version, path)] for every migration file, in version order."""
    directory = directory or MIGRATIONS_DIR
    if not os.path.isdir(directory):
        raise MigrationError(f"No migrations directory at {directory}")
    found = []
    for name in sorted(os.listdir(directory)):
        if name.startswith(".") or not name.endswith(".sql"):
            continue
        m = _FILENAME_RE.match(name)
        if not m:
            raise MigrationError(
                f"Migration {name!r} is not named NNNN_slug.sql, so its order "
                "is undefined")
        found.append((m.group(1), os.path.join(directory, name)))
    versions = [v for v, _ in found]
    dupes = {v for v in versions if versions.count(v) > 1}
    if dupes:
        raise MigrationError(f"Two migrations share version(s) {sorted(dupes)}")
    return found


def _checksum(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def applied(conn):
    """Return {version: checksum} for the migrations already applied."""
    with conn.cursor() as cur:
        cur.execute(TRACKING_TABLE_SQL)
        cur.execute("select version, checksum from schema_migrations")
        return dict(cur.fetchall())


def pending(conn, directory=None):
    """Return [(version, path)] not yet applied, in order.

    Raises if a migration already applied has been edited since: silently
    ignoring the edit would leave the database and the file disagreeing.
    """
    done = applied(conn)
    todo = []
    for version, path in discover(directory):
        if version in done:
            if done[version] != _checksum(path):
                raise MigrationError(
                    f"Migration {version} has changed since it was applied "
                    f"({os.path.basename(path)}). Add a new migration instead "
                    "of editing an applied one.")
            continue
        todo.append((version, path))
    return todo


def apply_pending(conn, directory=None, log=None):
    """Apply every pending migration in order; return the versions applied."""
    log = log or (lambda msg: None)
    done = []
    with conn.cursor() as cur:
        cur.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        for version, path in pending(conn, directory):
            name = os.path.basename(path)
            with open(path, "r", encoding="utf-8") as fh:
                sql = fh.read()
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "insert into schema_migrations (version, filename, checksum) "
                    "values (%s, %s, %s)",
                    (version, name, _checksum(path)))
            log(f"applied {name}")
            done.append(version)
    finally:
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
    return done


def main(argv=None):
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=None,
                   help="Postgres connection string. Env: DWB_DSN")
    p.add_argument("--dir", dest="directory", default=MIGRATIONS_DIR,
                   help="Directory of NNNN_slug.sql files (default migrations/)")
    p.add_argument("--status", action="store_true",
                   help="Report what is applied and what is pending; change nothing.")
    args = p.parse_args(argv)

    try:
        conn = db.connect(args.dsn)
    except db.DatabaseUnavailable as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        with conn:
            if args.status:
                done = applied(conn)
                for version, path in discover(args.directory):
                    state = "applied" if version in done else "pending"
                    print(f"{state:>7}  {os.path.basename(path)}")
                return 0
            applied_now = apply_pending(conn, args.directory, log=print)
            if applied_now:
                print(f"Applied {len(applied_now)} migration(s).")
            else:
                print(f"Nothing to apply ({len(applied(conn))} already applied).")
    except MigrationError as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
