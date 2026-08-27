"""Connecting to the Order store.

Hand-written SQL throughout, per ADR-0003, so this is a thin layer: it
resolves the connection string, supplies the password the string deliberately
omits, and turns a failure to connect into a message that says what to do.
"""

import sys

import psycopg
from psycopg import conninfo as _conninfo

from dwb.config import resolve_dsn, resolve_password


class DatabaseUnavailable(Exception):
    """The database could not be reached, with advice on the connection tried."""


# Distinguishes "the caller said nothing about a password" from "the caller
# said there is no password". Only the first should reach for the admin one.
FROM_ENVIRONMENT = object()


def connect(dsn=None, autocommit=False, connect_timeout=10,
            password=FROM_ENVIRONMENT):
    """Open a connection, or raise DatabaseUnavailable with a usable message.

    The password comes from POSTGRES_PASSWORD unless the connection string
    already carries one, or the caller passes one of its own. A caller
    connecting as anything other than the owning role must pass its own
    password — POSTGRES_PASSWORD is the admin account's, and silently
    borrowing it is how a read-only account stops being a separate account.
    """
    dsn = resolve_dsn(dsn)
    kwargs = {"autocommit": autocommit, "connect_timeout": connect_timeout}
    try:
        parsed = _conninfo.conninfo_to_dict(dsn)
    except psycopg.ProgrammingError as e:
        raise DatabaseUnavailable(f"Not a usable connection string: {e}")
    if not parsed.get("password"):
        if password is FROM_ENVIRONMENT:
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


def connect_or_exit(dsn=None, report=None):
    """Connect, or report why and return None.

    Every entry point opens its connection before doing any work — a run that
    fetches for twenty minutes and then finds it cannot store anything has
    wasted rate limit — and every one of them wants the same message when that
    fails, so it lives here once.
    """
    report = report or (lambda message: print(message, file=sys.stderr))
    try:
        return connect(dsn)
    except DatabaseUnavailable as e:
        report(str(e))
        return None


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
