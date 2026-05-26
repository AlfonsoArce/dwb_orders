#!/usr/bin/env python3
"""Group the distinct route_stop locations under each cost_center.

Walks the downloaded orders (one JSON file per order) and writes a workbook
with two sheets:

  - "summary"   one row per cost_center: order count, total stops, unique locations
  - "locations" one row per (cost_center, unique-location): with stop count and the
                companies seen at that address

A "location" is the tuple (address, city, state, postal_code), normalized to
uppercase with whitespace collapsed — the same definition used elsewhere in this
project.

    python3 route_stops_by_cost_center.py
    python3 route_stops_by_cost_center.py --input-dir output/orders --output output/route_stops_by_cost_center.xlsx
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict

import xlsxwriter
from tqdm import tqdm

EXCEL_CELL_LIMIT = 32_767
TRUNC_MARKER = "…[truncated]"
BLANK_COST_CENTER = "(blank)"  # surface orders with no cost_center instead of dropping them

logger = logging.getLogger("dwb_cost_center")


class TqdmLoggingHandler(logging.Handler):
    """Emit log records via tqdm.write so they don't corrupt the progress bar."""

    def emit(self, record):
        try:
            tqdm.write(self.format(record), file=sys.stderr)
            self.flush()
        except Exception:  # pragma: no cover
            self.handleError(record)


def setup_logging(level="INFO", log_file=None):
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    console = TqdmLoggingHandler()
    console.setLevel(lvl)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(lvl)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.info("Logging to file: %s", log_file)
    return logger


def norm(v):
    if v is None:
        return ""
    return " ".join(str(v).upper().split())


def list_order_files(input_dir):
    """Return order JSON paths sorted numerically by trailing order_number."""
    def key(name):
        stem = name[:-5] if name.endswith(".json") else name
        num = stem.rsplit("_", 1)[-1]
        try:
            return (0, int(num))
        except ValueError:
            return (1, name)

    out = []
    with os.scandir(input_dir) as it:
        for e in it:
            if e.is_file() and e.name.endswith(".json"):
                out.append(e.name)
    out.sort(key=key)
    return [os.path.join(input_dir, n) for n in out]


def truncate_cell(s):
    if len(s) > EXCEL_CELL_LIMIT:
        return s[: EXCEL_CELL_LIMIT - len(TRUNC_MARKER)] + TRUNC_MARKER
    return s


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input-dir", default="output/orders", help="Directory of order_*.json files")
    p.add_argument(
        "--output",
        default="output/route_stops_by_cost_center.xlsx",
        help="Path to the .xlsx file to write",
    )
    p.add_argument("--limit", type=int, default=0, help="Process only the first N order files (0 = all)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", default="logs/route_stops_by_cost_center.log")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    setup_logging(args.log_level, args.log_file)

    if not os.path.isdir(args.input_dir):
        logger.error("Input directory not found: %s", args.input_dir)
        return 2

    files = list_order_files(args.input_dir)
    if args.limit and args.limit > 0:
        files = files[: args.limit]
    logger.info("Found %d order files in %s", len(files), args.input_dir)
    if not files:
        logger.warning("Nothing to process — exiting")
        return 0

    # cost_center -> {
    #   "orders": int, "stops": int,
    #   "locations": dict[loc_key -> {"stops": int, "companies": set[str]}]
    # }
    per_cc = defaultdict(lambda: {"orders": 0, "stops": 0, "locations": defaultdict(lambda: {"stops": 0, "companies": set()})})
    failed_files = 0

    for path in tqdm(files, desc="Reading orders", unit="order"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                order = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            failed_files += 1
            logger.warning("Skipping %s: %s", path, e)
            continue

        cc = norm(order.get("cost_center")) or BLANK_COST_CENTER
        bucket = per_cc[cc]
        bucket["orders"] += 1

        for stop in order.get("route_stops") or []:
            addr = norm(stop.get("address"))
            city = norm(stop.get("city"))
            state = norm(stop.get("state"))
            zipc = norm(stop.get("postal_code"))
            # Skip wholly-empty stop locations (defensive — should be rare).
            if not (addr or city or state or zipc):
                continue
            key = (addr, city, state, zipc)
            loc = bucket["locations"][key]
            loc["stops"] += 1
            co = norm(stop.get("company"))
            if co:
                loc["companies"].add(co)
            bucket["stops"] += 1

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # constant_memory=False so we can write two sheets without re-opening.
    book = xlsxwriter.Workbook(args.output, {"strings_to_urls": False})
    bold = book.add_format({"bold": True})

    summary = book.add_worksheet("summary")
    summary.write_row(0, 0, ["cost_center", "total_orders", "total_stops", "unique_locations"], bold)
    summary.freeze_panes(1, 0)

    detail = book.add_worksheet("locations")
    detail.write_row(
        0,
        0,
        [
            "cost_center",
            "address",
            "city",
            "state",
            "postal_code",
            "stop_count",
            "distinct_companies",
            "companies",
        ],
        bold,
    )
    detail.freeze_panes(1, 0)

    # Summary sorted by total_stops desc so the busiest cost_centers float to the top.
    summary_rows = sorted(
        per_cc.items(),
        key=lambda kv: (-kv[1]["stops"], kv[0]),
    )
    for i, (cc, b) in enumerate(summary_rows, start=1):
        summary.write_row(i, 0, [cc, b["orders"], b["stops"], len(b["locations"])])

    # Detail grouped by cost_center (busiest first), each block sorted by stop_count desc.
    detail_row = 1
    for cc, b in summary_rows:
        locs = sorted(
            b["locations"].items(),
            key=lambda kv: (-kv[1]["stops"], kv[0]),
        )
        for (addr, city, state, zipc), loc in locs:
            companies = sorted(loc["companies"])
            detail.write_row(
                detail_row,
                0,
                [
                    cc,
                    addr,
                    city,
                    state,
                    zipc,
                    loc["stops"],
                    len(companies),
                    truncate_cell(" | ".join(companies)),
                ],
            )
            detail_row += 1

    book.close()
    logger.info(
        "Done. cost_centers: %d | orders: %d | failed files: %d | detail rows: %d",
        len(per_cc),
        sum(b["orders"] for b in per_cc.values()),
        failed_files,
        detail_row - 1,
    )
    logger.info("Excel file: %s", os.path.abspath(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
