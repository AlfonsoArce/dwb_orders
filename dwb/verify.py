"""Is the database a faithful copy of the JSON archive?

This is the evidence that lets the archive stop being the source of truth, so
it is deliberately paranoid and deliberately read-only. It does two things:

* counts — every Order file is read, its Order Number and Route Stop count
  taken, and the totals compared against the database;
* a sample — some Orders are reconstructed from the raw payload stored in the
  database and diffed against the file on disk, key by key.

Anything that does not line up is reported in enough detail to go and look,
and makes the check fail.
"""

import argparse
import glob
import json
import os
import random
import shutil
import sys

from tqdm import tqdm

from dwb import archive, coerce, db, ingest
from dwb.config import load_dotenv

DEFAULT_SAMPLE = 200

# Where ./backup.sh writes its dumps, and where this looks for proof that one
# exists before it will let the archive be retired.
DEFAULT_BACKUP_DIR = os.path.expanduser(
    os.environ.get("DWB_BACKUP_DIR", "~/dwb_orders_backups"))

# A path with any of these in it is inside a sync daemon's reach, which is the
# whole problem being escaped.
SYNCED_FOLDER_MARKERS = ("Mobile Documents", "Dropbox", "Google Drive")


class Report:
    """What was checked and what was found."""

    def __init__(self, input_dir, sample_size):
        self.input_dir = input_dir
        self.sample_size = sample_size
        self.files_found = 0
        self.problems = {name: 0 for name in archive.PROBLEMS}
        self.archive_orders = 0
        self.archive_stops = 0
        self.stored_orders = 0
        self.stored_stops = 0
        self.missing_from_database = []
        self.only_in_database = []
        self.stop_count_mismatches = []   # (order_number, in_file, in_database)
        self.sampled = []
        self.payload_mismatches = []      # (order_number, description)

    @property
    def ok(self):
        return not (self.missing_from_database or self.only_in_database
                    or self.stop_count_mismatches or self.payload_mismatches
                    or self.archive_orders != self.stored_orders
                    or self.archive_stops != self.stored_stops
                    or sum(self.problems.values()))

    def lines(self):
        yield f"Archive:            {self.input_dir}"
        yield f"Files found:        {self.files_found}"
        yield f"Files not read:     {sum(self.problems.values())}" \
              + ("   (unread means unverified, so the check fails)"
                 if sum(self.problems.values()) else "")
        for name in archive.PROBLEMS:
            yield f"  {name + ':':<16}{self.problems[name]}"
        yield f"Orders   in archive: {self.archive_orders:>9}   stored: {self.stored_orders:>9}"
        yield f"Stops    in archive: {self.archive_stops:>9}   stored: {self.stored_stops:>9}"
        yield f"Orders sampled and diffed against their file: {len(self.sampled)} " \
              f"(asked for {self.sample_size})"

        if self.missing_from_database:
            yield f"MISSING from the database: {len(self.missing_from_database)}"
            yield "  " + ", ".join(f"#{n}" for n in self.missing_from_database[:20])
        if self.only_in_database:
            yield f"IN THE DATABASE but not in the archive: {len(self.only_in_database)}"
            yield "  " + ", ".join(f"#{n}" for n in self.only_in_database[:20])
        for number, in_file, in_db in self.stop_count_mismatches[:20]:
            yield f"STOP COUNT differs for #{number}: file {in_file}, database {in_db}"
        for number, detail in self.payload_mismatches[:20]:
            yield f"PAYLOAD differs for #{number}: {detail}"

        yield "VERIFIED: the database matches the archive." if self.ok else \
              "FAILED: the database does not match the archive."


def _scan_archive(input_dir, report, show_progress=False):
    """Read every Order file; return {order_number: (filename, stop_count)}.

    Where sync left several copies of one Order, the newest revision wins —
    the same rule the import applied.
    """
    winners = {}
    revisions = {}
    # Sorts as a date, not as a string: the API sends timestamps in several
    # formats, and only one of them happens to sort correctly as text. The
    # import picks its winner the same way, so the two must agree.
    earliest = coerce.timestamp("1900-01-01 00:00:00")
    files = archive.list_order_files(input_dir)
    report.files_found = len(files)
    for name, _number in tqdm(files, desc="Reading archive", unit="file",
                              disable=not show_progress):
        order, problem = archive.read_order_file(os.path.join(input_dir, name))
        if problem:
            report.problems[problem] += 1
            continue
        number = int(order["order_number"])
        revision = coerce.timestamp(order.get("version")) or earliest
        if number in winners and revisions[number] >= revision:
            continue
        winners[number] = (name, len(order.get("route_stops") or []))
        revisions[number] = revision
    return winners


def _stored(conn, source_key):
    """Return {order_number: stop_count} from the database."""
    with conn.cursor() as cur:
        cur.execute("""
            select o.order_number, count(rs.route_stop_id)
            from orders o
            left join route_stops rs on rs.order_id = o.order_id
            where o.source_key = %s
            group by o.order_number
        """, (source_key,))
        return {row[0]: row[1] for row in cur.fetchall()}


def _describe_difference(stored, on_disk):
    """Say how two payloads differ, briefly enough to act on."""
    if not isinstance(stored, dict) or not isinstance(on_disk, dict):
        return "stored payload is not an object"
    missing = sorted(set(on_disk) - set(stored))
    extra = sorted(set(stored) - set(on_disk))
    changed = sorted(k for k in set(stored) & set(on_disk) if stored[k] != on_disk[k])
    parts = []
    if missing:
        parts.append(f"missing keys {missing[:5]}")
    if extra:
        parts.append(f"unexpected keys {extra[:5]}")
    if changed:
        parts.append(f"differing values for {changed[:5]}")
    return "; ".join(parts) or "payloads differ"


def verify(conn, input_dir, source_key=ingest.DEFAULT_SOURCE,
           sample_size=DEFAULT_SAMPLE, seed=None, show_progress=False):
    """Compare the database against the archive. Changes nothing."""
    input_dir = os.path.abspath(input_dir)
    report = Report(input_dir, sample_size)

    winners = _scan_archive(input_dir, report, show_progress)
    report.archive_orders = len(winners)
    report.archive_stops = sum(stops for _, stops in winners.values())

    stored = _stored(conn, source_key)
    report.stored_orders = len(stored)
    report.stored_stops = sum(stored.values())

    report.missing_from_database = sorted(set(winners) - set(stored))
    report.only_in_database = sorted(set(stored) - set(winners))
    for number in sorted(set(winners) & set(stored)):
        if winners[number][1] != stored[number]:
            report.stop_count_mismatches.append(
                (number, winners[number][1], stored[number]))

    common = sorted(set(winners) & set(stored))
    rng = random.Random(seed)
    sample = rng.sample(common, min(sample_size, len(common)))
    report.sampled = sorted(sample)
    if sample:
        with conn.cursor() as cur:
            cur.execute("select order_number, raw from orders "
                        "where source_key = %s and order_number = any(%s)",
                        (source_key, sample))
            payloads = dict(cur.fetchall())
        for number in sample:
            name, _stops = winners[number]
            with open(os.path.join(input_dir, name), "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
            if payloads.get(number) != on_disk:
                report.payload_mismatches.append(
                    (number, _describe_difference(payloads.get(number), on_disk)))
    return report


class RetirementRefused(Exception):
    """The archive was not moved, and why."""


def retire_archive(report, input_dir, destination, backup_dir=None, confirmed=False):
    """Move the archive to cold storage, once it is safe to.

    Moved, never deleted: this is 764 MB of Orders that were the only copy
    until very recently. Three things have to hold first — the verification
    passed, a dump of the database exists, and the destination is somewhere the
    sync daemon cannot reach.
    """
    backup_dir = backup_dir or DEFAULT_BACKUP_DIR
    destination = os.path.abspath(os.path.expanduser(destination))

    if not report.ok:
        raise RetirementRefused(
            "Verification failed, so the archive stays where it is.")
    dumps = sorted(glob.glob(os.path.join(os.path.expanduser(backup_dir), "*.dump")))
    if not dumps:
        raise RetirementRefused(
            f"No database dump found in {backup_dir}. Take one first:  ./backup.sh dump")
    for marker in SYNCED_FOLDER_MARKERS:
        if marker in destination:
            raise RetirementRefused(
                f"{destination} is inside a synced folder; that is what we are "
                "moving the archive out of.")
    if os.path.exists(destination) and os.listdir(destination):
        raise RetirementRefused(f"{destination} already exists and is not empty.")

    plan = (f"Move {report.files_found} file(s)\n"
            f"  from {input_dir}\n"
            f"  to   {destination}\n"
            f"  most recent dump: {dumps[-1]}")
    if not confirmed:
        return plan + "\nNothing moved. Add --yes to do it."

    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    shutil.move(input_dir, destination)
    # The exporters and the fetcher still expect the folder to be there, and
    # the repository tracks its layout.
    os.makedirs(input_dir, exist_ok=True)
    open(os.path.join(input_dir, ".gitkeep"), "a").close()
    return plan + "\nMoved. The archive is now cold storage, outside the synced folder."


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", default="output/orders",
                        help="The Order archive to check against (default output/orders)")
    parser.add_argument("--dsn", default=None,
                        help="Postgres connection string. Env: DWB_DSN")
    parser.add_argument("--source", default=ingest.DEFAULT_SOURCE,
                        help=f"Which Source to check (default {ingest.DEFAULT_SOURCE})")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, metavar="N",
                        help=f"Orders to reconstruct and diff (default {DEFAULT_SAMPLE})")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed the sample, to repeat an earlier check exactly.")
    parser.add_argument("--progress", dest="progress", action="store_true",
                        default=sys.stderr.isatty(), help="Show a progress bar.")
    parser.add_argument("--no-progress", dest="progress", action="store_false",
                        help="Never show a progress bar.")
    parser.add_argument("--retire-to", default=None, metavar="DIR",
                        help="If the check passes and a database dump exists, move "
                             "the archive here — outside the synced folder. Reports "
                             "the plan unless --yes is given.")
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR,
                        help=f"Where ./backup.sh writes dumps (default {DEFAULT_BACKUP_DIR})")
    parser.add_argument("--yes", action="store_true",
                        help="Actually move the archive, rather than describing it.")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.input_dir):
        print(f"No such directory: {args.input_dir}", file=sys.stderr)
        return 2
    conn = db.connect_or_exit(args.dsn)
    if conn is None:
        return 2

    try:
        with conn:
            # Read-only, and said so out loud: this check must never be able to
            # change what it is checking.
            with conn.cursor() as cur:
                cur.execute("set transaction read only")
            report = verify(conn, args.input_dir, args.source, args.sample,
                            args.seed, args.progress)
    finally:
        conn.close()

    for line in report.lines():
        print(line)

    if args.retire_to:
        try:
            print(retire_archive(report, os.path.abspath(args.input_dir),
                                 args.retire_to, args.backup_dir, args.yes))
        except RetirementRefused as e:
            print(f"Archive not retired: {e}", file=sys.stderr)
            return 1
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
