"""Load the JSON Order archive into the store, once, resumably.

Reading the files is the slow part: they sit in a synced folder that is
actively re-enumerated and some may need downloading on access. So work goes
in batches, each batch commits, and each batch records the file it reached.
An interrupted run resumes from there — overlapping a little, which the
revision-guarded upsert makes free.

Damaged files are counted, never silently skipped. An empty file, an
unreadable file and a file that vanished mid-run are three different problems,
and a summary that hid them would misreport the import as complete.
"""

import argparse
import os
import sys

from tqdm import tqdm

from dwb import archive, db, ingest
from dwb.config import load_dotenv

DEFAULT_INPUT_DIR = "output/orders"
DEFAULT_BATCH_SIZE = 500


class Summary:
    """What an import run did, honestly."""

    def __init__(self):
        self.files_found = 0
        self.files_skipped = 0     # already done in an earlier run
        self.files_read = 0
        self.orders_loaded = 0
        self.stops_loaded = 0
        self.problems = {name: 0 for name in archive.PROBLEMS}

    @property
    def files_not_read(self):
        return sum(self.problems.values())

    def lines(self):
        yield f"Files found:        {self.files_found}"
        if self.files_skipped:
            yield f"Files already done: {self.files_skipped} (resumed)"
        yield f"Files read:         {self.files_read}"
        yield f"Orders loaded:      {self.orders_loaded}"
        yield f"Route Stops loaded: {self.stops_loaded}"
        yield f"Files not read:     {self.files_not_read}"
        for name in archive.PROBLEMS:
            yield f"  {name + ':':<16}{self.problems[name]}"


def read_progress(conn, source_key, input_dir):
    """Return the last filename this directory's import reached, or None."""
    with conn.cursor() as cur:
        cur.execute("select last_file from import_progress "
                    "where source_key = %s and input_dir = %s",
                    (source_key, input_dir))
        row = cur.fetchone()
    return row[0] if row else None


def clear_progress(conn, source_key, input_dir):
    with conn.cursor() as cur:
        cur.execute("delete from import_progress "
                    "where source_key = %s and input_dir = %s",
                    (source_key, input_dir))


def record_progress(conn, source_key, input_dir, last_file, files, orders, stops):
    with conn.cursor() as cur:
        cur.execute("""
            insert into import_progress (source_key, input_dir, last_file,
                                         files_done, orders_loaded, stops_loaded)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (source_key, input_dir) do update set
                last_file = excluded.last_file,
                files_done = import_progress.files_done + excluded.files_done,
                orders_loaded = import_progress.orders_loaded + excluded.orders_loaded,
                stops_loaded = import_progress.stops_loaded + excluded.stops_loaded,
                updated_at = now()
        """, (source_key, input_dir, last_file, files, orders, stops))


def import_archive(conn, input_dir, source_key=ingest.DEFAULT_SOURCE,
                   batch_size=DEFAULT_BATCH_SIZE, limit=0, restart=False,
                   show_progress=False):
    """Import every Order file in input_dir. Returns a Summary."""
    input_dir = os.path.abspath(input_dir)
    summary = Summary()

    if restart:
        clear_progress(conn, source_key, input_dir)
        conn.commit()
    resume_after = read_progress(conn, source_key, input_dir)

    files = archive.list_order_files(input_dir)
    summary.files_found = len(files)
    if resume_after is not None:
        remaining = [f for f in files if f[0] > resume_after]
        summary.files_skipped = len(files) - len(remaining)
        files = remaining
    if limit:
        files = files[:limit]

    bar = tqdm(total=len(files), unit="file", desc="Importing",
               disable=not show_progress)
    batch = []
    last_file = None

    def flush():
        nonlocal batch, last_file
        if not batch and last_file is None:
            return
        result = ingest.ingest_orders(conn, batch, source_key)
        summary.orders_loaded += result.orders_written
        summary.stops_loaded += result.stops_written
        record_progress(conn, source_key, input_dir, last_file,
                        len(batch), result.orders_written, result.stops_written)
        conn.commit()
        batch = []

    try:
        for name, _number in files:
            order, problem = archive.read_order_file(os.path.join(input_dir, name))
            last_file = name
            if problem:
                summary.problems[problem] += 1
            else:
                summary.files_read += 1
                batch.append(order)
            bar.update(1)
            if len(batch) >= batch_size:
                flush()
        flush()
    finally:
        bar.close()
    return summary


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR,
                        help=f"Directory of order_*.json files (default {DEFAULT_INPUT_DIR})")
    parser.add_argument("--dsn", default=None,
                        help="Postgres connection string. Env: DWB_DSN")
    parser.add_argument("--source", default=ingest.DEFAULT_SOURCE,
                        help="Which dispatch system these Orders came from "
                             f"(default {ingest.DEFAULT_SOURCE})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Orders per transaction (default {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after this many files (0 = all). Progress is "
                             "recorded either way, so the next run continues.")
    parser.add_argument("--restart", action="store_true",
                        help="Ignore recorded progress and read every file again.")
    parser.add_argument("--progress", dest="progress", action="store_true",
                        default=sys.stderr.isatty(),
                        help="Show a progress bar (default: only on a terminal).")
    parser.add_argument("--no-progress", dest="progress", action="store_false",
                        help="Never show a progress bar.")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.input_dir):
        print(f"No such directory: {args.input_dir}", file=sys.stderr)
        return 2

    conn = db.connect_or_exit(args.dsn)
    if conn is None:
        return 2

    try:
        with conn:
            summary = import_archive(
                conn, args.input_dir, source_key=args.source,
                batch_size=args.batch_size, limit=args.limit,
                restart=args.restart, show_progress=args.progress)
    finally:
        conn.close()

    for line in summary.lines():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
