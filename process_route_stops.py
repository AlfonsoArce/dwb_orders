#!/usr/bin/env python3
"""Flatten every route_stop from the downloaded orders into a single Excel file.

Reads JSON files produced by get_orders.py (one order per file) and writes one
row per route_stop, prefixed with order-level context, to an .xlsx file.

    python3 process_route_stops.py
    python3 process_route_stops.py --input-dir output/orders --output output/route_stops.xlsx
"""

import argparse
import json
import logging
import os
import sys

import xlsxwriter
from tqdm import tqdm

# The signature_lines SVG can be tens of KB per stop — useless in a spreadsheet
# and would push xlsx file size into the GBs. Drop it.
DROPPED_STOP_FIELDS = ("signature_lines",)

# Excel cells max out at 32,767 chars; we truncate longer text with a marker so
# the write does not abort mid-file on an outlier row.
EXCEL_CELL_LIMIT = 32_767
TRUNC_MARKER = "…[truncated]"

ORDER_FIELDS = (
    "id",
    "order_number",
    "time",
    "status",
    "status_date",
    "status_detail",
    "origin",
    "order_type",
    "price",
    "final_price",
    "customer_number",
    "cost_center",
    "dispatch_driver",
    "ready_time",
    "deliver_by",
    "flagged",
    "read",
    "pending",
    "comm_override",
    "recurring_name",
    "optimized_route",
    "version",
)

STOP_FIELDS = (
    "route_stop_id",
    "company",
    "address",
    "suite",
    "city",
    "state",
    "postal_code",
    "country",
    "service_type",
    "package",
    "number_of_pieces",
    "weight",
    "vehicle",
    "driver_number",
    "paper_waybill",
    "special_instructions",
    "return_add",
    "dispatch_message",
    "notes",
    "signature_contact",
    "reference",
    "signature",
    "fuel_surcharge",
    "route_status",
    "route_status_detail",
    "route_status_date",
    "distance",
    "air_distance",
    "driver_pricelist",
    "receive_date",
    "dispatch_date",
    "pickup_date",
    "delivery_date",
    "cancel_date",
    "confirm_date",
)

def _order_col(k):
    # Avoid awkward doubled prefixes like "order_order_number".
    return k if k.startswith("order_") else f"order_{k}"


# Final column order: order_*, then stop position, then stop_*, then contact + packages.
COLUMNS = (
    [_order_col(k) for k in ORDER_FIELDS]
    + ["stop_index", "stop_count"]
    + list(STOP_FIELDS)
    + ["contact_name", "contact_phone", "packages_json"]
)

logger = logging.getLogger("dwb_route_stops")


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


def list_order_files(input_dir):
    """Return order JSON paths sorted numerically by order_number in the filename."""
    def key(name):
        stem = name[:-5] if name.endswith(".json") else name
        num = stem.rsplit("_", 1)[-1]
        try:
            return (0, int(num))
        except ValueError:
            return (1, name)  # non-numeric names sort after, alphabetically

    entries = []
    with os.scandir(input_dir) as it:
        for e in it:
            if e.is_file() and e.name.endswith(".json"):
                entries.append(e.name)
    entries.sort(key=key)
    return [os.path.join(input_dir, n) for n in entries]


def cell_value(v):
    """Coerce a JSON value into something xlsxwriter can write to a cell."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, str)):
        if isinstance(v, str) and len(v) > EXCEL_CELL_LIMIT:
            return v[: EXCEL_CELL_LIMIT - len(TRUNC_MARKER)] + TRUNC_MARKER
        return v
    # Lists/dicts that aren't explicitly handled get JSON-serialized as a fallback.
    s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if len(s) > EXCEL_CELL_LIMIT:
        s = s[: EXCEL_CELL_LIMIT - len(TRUNC_MARKER)] + TRUNC_MARKER
    return s


def build_row(order, stop, stop_index, stop_count):
    row = []
    for k in ORDER_FIELDS:
        row.append(cell_value(order.get(k)))
    row.append(stop_index)
    row.append(stop_count)
    for k in STOP_FIELDS:
        if k in DROPPED_STOP_FIELDS:
            row.append("")
            continue
        row.append(cell_value(stop.get(k)))
    contact = stop.get("contact") or {}
    row.append(cell_value(contact.get("name")))
    row.append(cell_value(contact.get("phone")))
    packages = stop.get("packages")
    row.append(cell_value(packages) if packages is not None else "")
    return row


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input-dir", default="output/orders", help="Directory containing order_*.json files")
    p.add_argument("--output", default="output/route_stops.xlsx", help="Path to the .xlsx file to write")
    p.add_argument("--sheet-name", default="route_stops", help="Worksheet name")
    p.add_argument("--limit", type=int, default=0, help="Process only the first N order files (0 = all)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", default="logs/process_route_stops.log")
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

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # constant_memory streams rows to disk and discards them — required at this row count.
    # The flip side: rows must be written in order and the sheet can't be re-edited.
    workbook = xlsxwriter.Workbook(args.output, {"constant_memory": True, "strings_to_urls": False})
    sheet = workbook.add_worksheet(args.sheet_name)
    header_fmt = workbook.add_format({"bold": True})
    sheet.write_row(0, 0, COLUMNS, header_fmt)
    sheet.freeze_panes(1, 0)

    row_idx = 1
    stops_written = 0
    orders_with_no_stops = 0
    failed_files = 0

    for path in tqdm(files, desc="Flattening orders", unit="order"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                order = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            failed_files += 1
            logger.warning("Skipping %s: %s", path, e)
            continue

        stops = order.get("route_stops") or []
        if not stops:
            orders_with_no_stops += 1
            continue

        stop_count = len(stops)
        for i, stop in enumerate(stops, start=1):
            row = build_row(order, stop, i, stop_count)
            sheet.write_row(row_idx, 0, row)
            row_idx += 1
            stops_written += 1

    workbook.close()
    logger.info(
        "Done. Orders processed: %d | stops written: %d | empty orders: %d | failed files: %d",
        len(files) - failed_files,
        stops_written,
        orders_with_no_stops,
        failed_files,
    )
    logger.info("Excel file: %s", os.path.abspath(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
