"""Connecting to the Order store.

Hand-written SQL throughout, per ADR-0003, so this is a thin layer: it
resolves the connection string, supplies the password the string deliberately
omits, and turns a failure to connect into a message that says what to do.
"""

import psycopg
from psycopg import conninfo as _conninfo

from dwb.config import resolve_dsn, resolve_password


class DatabaseUnavailable(Exception):
    """The database could not be reached, with advice on the connection tried."""


def connect(dsn=None, autocommit=False, connect_timeout=10):
    """Open a connection, or raise DatabaseUnavailable with a usable message.

    The password comes from POSTGRES_PASSWORD unless the connection string
    already carries one of its own.
    """
    dsn = resolve_dsn(dsn)
    kwargs = {"autocommit": autocommit, "connect_timeout": connect_timeout}
    try:
        parsed = _conninfo.conninfo_to_dict(dsn)
    except psycopg.ProgrammingError as e:
        raise DatabaseUnavailable(f"Not a usable connection string: {e}")
    if not parsed.get("password"):
        password = resolve_password()
        if password:
            kwargs["password"] = password
    try:
        return psycopg.connect(dsn, **kwargs)
    except psycopg.OperationalError as e:
        raise DatabaseUnavailable(
            f"Cannot reach the Order store at {safe_dsn(dsn)}: {str(e).strip()}\n"
            "Is it running? Start it with:  docker compose up -d"
        )


def safe_dsn(dsn):
    """Return the connection string with any embedded password removed.

    Connection strings get logged; passwords must not be.
    """
    try:
        parsed = _conninfo.conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        return "(unparseable connection string)"
    parsed.pop("password", None)
    return _conninfo.make_conninfo(**parsed)
