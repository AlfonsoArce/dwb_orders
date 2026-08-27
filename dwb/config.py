"""Settings that arrive by flag, environment variable, or .env file.

The precedence is the one the API credentials have always used: a command-line
flag beats an environment variable, which beats a line in .env. load_dotenv()
implements the second half of that by refusing to overwrite anything already
in the real environment; the flag half is each entry point's argparse default.
"""

import os

# Where Postgres listens when nothing says otherwise: the port docker-compose
# publishes, on loopback. No password — see resolve_password().
DEFAULT_DSN = "postgresql://dwb@127.0.0.1:5434/dwb_orders"

# The same server, as the Orders viewer's read-only role. Also passwordless
# here — see resolve_viewer_password(), which is not resolve_password().
DEFAULT_VIEWER_DSN = "postgresql://dwb_viewer@127.0.0.1:5434/dwb_orders"


def load_dotenv(path=".env"):
    """Minimal .env loader (no external deps).

    Reads KEY=VALUE lines and sets them in os.environ without overriding
    variables that are already set in the real environment.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def resolve_dsn(flag=None):
    """Return the Postgres connection string: flag, then env, then default.

    .env is folded into the environment by load_dotenv() before this is called,
    so "then env" covers the dotenv case too.
    """
    return flag or os.environ.get("DWB_DSN") or DEFAULT_DSN


def resolve_password():
    """Return the database password, or None if none is configured.

    The password is kept out of the connection string so that the same secret
    isn't maintained in two formats — docker-compose reads POSTGRES_PASSWORD
    and so do we. PGPASSWORD is honoured as well, since libpq users expect it.
    """
    return os.environ.get("POSTGRES_PASSWORD") or os.environ.get("PGPASSWORD") or None


def resolve_viewer_dsn(flag=None):
    """Return the Orders viewer's connection string: flag, then env, default.

    Separate from resolve_dsn() because it is a different account and, inside
    the compose network, a different address: the database answers there on
    its service name and internal port, not on the loopback port it publishes.
    """
    return flag or os.environ.get("DWB_VIEWER_DSN") or DEFAULT_VIEWER_DSN


def resolve_viewer_password():
    """Return the viewer role's password, or None if none is configured.

    Deliberately does not fall back to resolve_password(). That one returns
    POSTGRES_PASSWORD — the owning account's — and a viewer that borrows it
    connects as itself with the admin's secret, which is not a mistake worth
    making convenient. See migration 0006 for how the role gets a password.
    """
    return os.environ.get("DWB_VIEWER_PASSWORD") or None
