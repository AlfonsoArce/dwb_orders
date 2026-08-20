"""Reading the JSON Order archive on disk.

The archive is one file per Order in a folder the sync daemon is continuously
re-enumerating, so two things need care. Sync produces conflict copies —
"order_401791 2.json" — which the fetcher's own filename pattern does not
match and which therefore went unnoticed for a long time; they are matched
here. And a file can be unreadable, empty, or gone between listing it and
opening it, which are three different problems and get counted as three.
"""

import json
import os
import re

from dwb import coerce

# order_<number>.json, plus whatever the sync daemon appended to the stem when
# it made a conflict copy (" 2", " (Case Conflict 3)", " copy").
ORDER_FILE_RE = re.compile(r"^order_(?P<number>\d+)(?P<conflict>[ (].*)?\.json$",
                           re.IGNORECASE)

# Why a file could not be turned into an Order. Counted separately, never
# silently skipped: a summary that hid them would misreport an import as
# complete.
VANISHED = "vanished"      # listed, then gone before it could be read
EMPTY = "empty"            # zero bytes, or nothing but whitespace
UNREADABLE = "unreadable"  # I/O error, bad encoding, or malformed JSON
UNUSABLE = "unusable"      # readable JSON, but no usable Order Number to key it by

PROBLEMS = (VANISHED, EMPTY, UNREADABLE, UNUSABLE)


def list_order_files(directory):
    """Return [(filename, order_number)] for every Order file, sorted by name.

    Sorted so that a resumed import can skip everything up to where it left
    off, and so two runs see the same order.
    """
    found = []
    with os.scandir(directory) as entries:
        for entry in entries:
            match = ORDER_FILE_RE.match(entry.name)
            if match and not entry.is_dir():
                found.append((entry.name, int(match.group("number"))))
    found.sort()
    return found


def read_order_file(path):
    """Return (order, problem). Exactly one of the two is None."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return None, VANISHED
    except OSError:
        return None, UNREADABLE

    if not data.strip():
        return None, EMPTY

    try:
        order = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, UNREADABLE

    # Without an Order Number there is nothing to key the Order by, and a
    # non-numeric one would fail at the column rather than be counted here.
    if not isinstance(order, dict) or coerce.integer(order.get("order_number")) is None:
        return None, UNUSABLE
    return order, None
