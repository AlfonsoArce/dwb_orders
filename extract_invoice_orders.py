#!/usr/bin/env python3
"""Extract orders from HawkExpress invoice PDFs into an Excel workbook.

Walks input/invoices/<vendor>/*.pdf, parses each invoice's line-item table by
word position (pdfplumber's table mode is inconsistent across these PDFs —
sometimes one big row, sometimes per-order rows), and writes three sheets:

    invoices     — one row per invoice header
    orders       — one row per order line (#NNNNNN), with per-order discount
    adjustments  — invoice-level lines without an order # (e.g. global discount,
                   waiting time per minute)

    python3 extract_invoice_orders.py
    python3 extract_invoice_orders.py --input-dir input/invoices --output output/invoice_orders.xlsx
"""

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import xlsxwriter
from tqdm import tqdm

LOG = logging.getLogger("extract_invoice_orders")

# Header row of the line-item table; used to locate column boundaries.
HEADER_TOKENS = ("Date", "Order", "Info", "Service", "Reference", "Total")
# Row-grouping tolerance for word top-coordinates (points).
ROW_Y_TOLERANCE = 3.0
# Order-number marker, e.g. "#380123 From: ...". Per the invoice template, the
# order # always precedes "From:", so we require that lookahead to avoid catching
# stray #-prefixed references (PO numbers, etc.).
ORDER_NUM_RE = re.compile(r"#(\d{4,})(?=\s+From:)")
# Currency-like amount: 48.00, -52.32, 1,234.56, etc.
AMOUNT_RE = re.compile(r"^-?\$?[\d,]+\.\d{2}$")
# Recognised service codes; helps validate column assignment.
SERVICE_HINTS = ("VAN", "SPRINTER", "TRUCK", "CARGO", "BOX", "SUV", "CAR", "STRAIGHT")


@dataclass
class InvoiceHeader:
    vendor: str
    invoice_number: str = ""
    invoice_date: str = ""
    due_date: str = ""
    terms: str = ""
    bill_to: str = ""
    total: str = ""
    balance_due: str = ""
    source_file: str = ""


@dataclass
class Order:
    vendor: str
    invoice_number: str
    invoice_date: str
    order_date: str = ""
    order_number: str = ""
    from_party: str = ""
    to_party: str = ""
    service: str = ""
    reference: str = ""
    amount: str = ""
    discount_label: str = ""
    discount_amount: str = ""
    raw_order_info: str = ""
    source_file: str = ""


@dataclass
class Adjustment:
    vendor: str
    invoice_number: str
    invoice_date: str
    adjustment_date: str = ""
    description: str = ""
    service: str = ""
    reference: str = ""
    amount: str = ""
    source_file: str = ""


@dataclass
class ParsedInvoice:
    header: InvoiceHeader
    orders: list = field(default_factory=list)
    adjustments: list = field(default_factory=list)


def find_invoice_pdfs(root):
    """Yield (vendor_folder_name, pdf_path) for every invoice PDF under root."""
    for vendor_dir in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        for pdf in sorted(vendor_dir.glob("*.pdf")):
            yield vendor_dir.name, pdf


def _words_by_row(words, y_tol=ROW_Y_TOLERANCE):
    """Group words into visual rows based on their top y-coordinate."""
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(w["top"] - rows[-1][0]["top"]) <= y_tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    # Sort each row left-to-right.
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


COL_NAMES_5 = ("date", "order_info", "service", "reference", "total")


def _find_line_item_table(page):
    """Return the pdfplumber Table whose first row matches the line-item header,
    or None if not found on this page."""
    for tbl in page.find_tables():
        if len(tbl.columns) != 5 or len(tbl.rows) < 1:
            continue
        # Header row text spans the full first row of the table.
        header_row = tbl.rows[0]
        y0, y1 = header_row.bbox[1], header_row.bbox[3]
        words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
        header_text = " ".join(
            w["text"] for w in words if y0 - 1 <= w["top"] <= y1 + 1
        )
        if all(tok in header_text for tok in HEADER_TOKENS):
            return tbl
    return None


def _column_bounds(table):
    """Return {col_name: (x_min, x_max)} using the table's visible column edges."""
    cols_sorted = sorted(table.columns, key=lambda c: c.bbox[0])
    return {
        name: (col.bbox[0], col.bbox[2])
        for name, col in zip(COL_NAMES_5, cols_sorted)
    }


def _assign_to_columns(row, bounds):
    """Distribute words in a visual row into the line-item columns."""
    cells = {name: [] for name in bounds}
    for w in row:
        cx = (w["x0"] + w["x1"]) / 2
        for name, (lo, hi) in bounds.items():
            if lo <= cx < hi:
                cells[name].append(w["text"])
                break
    return {name: " ".join(parts).strip() for name, parts in cells.items()}


def _region_text(page, bbox):
    """Concatenate words inside bbox left-to-right, top-to-bottom; lines joined
    with newlines."""
    x0, top, x1, bottom = bbox
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    inside = [
        w for w in words
        if w["x0"] >= x0 - 1 and w["x1"] <= x1 + 1
        and w["top"] >= top - 1 and w["bottom"] <= bottom + 1
    ]
    rows = _words_by_row(inside)
    return "\n".join(" ".join(w["text"] for w in row).strip() for row in rows)


def _extract_header_fields(page, vendor, source_file):
    """Pull invoice number, dates, bill-to, totals from page-1 layout."""
    header = InvoiceHeader(vendor=vendor, source_file=source_file)

    # Use the page's narrow header tables: Bill To (left) and Date/Invoice (right).
    line_item_tbl = _find_line_item_table(page)
    line_item_top = line_item_tbl.bbox[1] if line_item_tbl else page.height
    header_tables = [
        t for t in page.find_tables()
        if t.bbox[3] <= line_item_top + 1 and t is not line_item_tbl
    ]

    for tbl in header_tables:
        cells = tbl.extract()
        if not cells:
            continue
        flat = " ".join(
            " ".join(filter(None, row)) for row in cells if row
        )
        if "Bill To" in flat:
            # Cells: [["Bill To:"], ["<vendor>\n<addr1>\n<addr2>"]]
            body_lines = []
            for row in cells:
                for cell in row:
                    if cell and "Bill To" not in cell:
                        body_lines.extend(
                            line.strip() for line in cell.splitlines() if line.strip()
                        )
            header.bill_to = " / ".join(body_lines)
        elif "Invoice" in flat and "Date" in flat:
            # Two-row table: header ["Date","Invoice"], values ["m/d/y","#####"].
            if len(cells) >= 2 and len(cells[1]) >= 2:
                header.invoice_date = (cells[1][0] or "").strip()
                header.invoice_number = (cells[1][1] or "").strip()

    # Terms / Due Date live in a free-text area to the right of Bill To.
    # Read the right half of the page from below the Date/Invoice table.
    right_top = max(
        (t.bbox[3] for t in header_tables if "Invoice" in _region_text(page, t.bbox)),
        default=160,
    )
    right_text = _region_text(page, (page.width * 0.6, right_top, page.width, line_item_top))
    terms_match = re.search(r"Terms\s+(.+)", right_text)
    if terms_match:
        header.terms = terms_match.group(1).strip()
    due_match = re.search(r"Due Date\s+(\d{1,2}/\d{1,2}/\d{2,4})", right_text)
    if due_match:
        header.due_date = due_match.group(1)
    return header


def _is_row_start(cells):
    """A visual row starts a new logical entry if it carries the start of a new
    order. Per-order DISCOUNT lines (no date, no order#) are a continuation of
    the order block above. Invoice-level adjustment rows (DISCOUNT or WAITING
    TIME with their own date) are their own row."""
    order_info = cells.get("order_info", "")
    has_date = bool(re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", cells.get("date", "")))
    has_total = bool(AMOUNT_RE.match((cells.get("total") or "").replace(" ", "")))
    has_order_num = bool(ORDER_NUM_RE.search(order_info))
    is_dateless_discount = (
        re.match(r"^\s*DISCOUNT\b", order_info, re.IGNORECASE)
        and not has_date
        and not has_order_num
    )
    if is_dateless_discount:
        return False
    return has_date or has_total or has_order_num


def _parse_order_info(text):
    """Split '#380123 From: X, To: Y' into (order_number, from, to). Any of the
    fields may be missing — return '' for missing parts."""
    order_number = ""
    from_party = ""
    to_party = ""
    m = ORDER_NUM_RE.search(text)
    if m:
        order_number = m.group(1)
    fm = re.search(r"From:\s*(.+?)\s*,\s*To:\s*(.+)$", text, re.DOTALL)
    if fm:
        from_party = re.sub(r"\s+", " ", fm.group(1)).strip()
        to_party = re.sub(r"\s+", " ", fm.group(2)).strip()
    return order_number, from_party, to_party


def _commit_block(block, header):
    """Turn a list of accumulated visual-row cell-dicts into Order/Adjustment
    records. A block is the set of visual rows belonging to one logical entry
    (i.e. starting at a row-start and ending before the next row-start)."""
    if not block:
        return [], []

    # Merge all visual rows in the block into combined cells.
    combined = {"date": "", "order_info": "", "service": "", "reference": "", "total": ""}
    totals = []  # capture every numeric Total in the block, in order
    for cells in block:
        for col, text in cells.items():
            if not text:
                continue
            if col == "total":
                # The Total column can have multiple amounts in a block
                # (line total + discount). Track each separately.
                for tok in text.split():
                    if AMOUNT_RE.match(tok.replace(" ", "")):
                        totals.append(tok)
            else:
                combined[col] = (combined[col] + " " + text).strip() if combined[col] else text

    order_info = combined["order_info"]
    has_order_num = bool(ORDER_NUM_RE.search(order_info))

    # Pull out a trailing "DISCOUNT XX%" or "WAITING TIME ..." label if present.
    discount_label = ""
    discount_amount = ""
    disc_match = re.search(r"DISCOUNT\s+\d+\s*%?", order_info)
    if disc_match:
        discount_label = disc_match.group(0)
        order_info = (order_info[: disc_match.start()] + order_info[disc_match.end():]).strip()
        # The discount amount is the negative total in the block (if any),
        # otherwise the second total.
        neg_totals = [t for t in totals if t.startswith("-")]
        if neg_totals:
            discount_amount = neg_totals[0]
            totals = [t for t in totals if t != discount_amount]
        elif len(totals) >= 2:
            discount_amount = totals[-1]
            totals = totals[:-1]

    waiting_match = re.search(r"\bWAITING TIME[^\n]*", order_info, re.IGNORECASE)
    waiting_label = ""
    waiting_amount = ""
    if waiting_match:
        waiting_label = waiting_match.group(0).strip()
        order_info = (order_info[: waiting_match.start()] + order_info[waiting_match.end():]).strip()
        if totals:
            waiting_amount = totals[-1]
            totals = totals[:-1]

    amount = totals[0] if totals else ""

    orders = []
    adjustments = []

    if has_order_num:
        order_number, from_party, to_party = _parse_order_info(order_info)
        orders.append(
            Order(
                vendor=header.vendor,
                invoice_number=header.invoice_number,
                invoice_date=header.invoice_date,
                order_date=combined["date"],
                order_number=order_number,
                from_party=from_party,
                to_party=to_party,
                service=combined["service"],
                reference=combined["reference"],
                amount=amount,
                discount_label=discount_label,
                discount_amount=discount_amount,
                raw_order_info=order_info,
                source_file=header.source_file,
            )
        )
        if waiting_label:
            adjustments.append(
                Adjustment(
                    vendor=header.vendor,
                    invoice_number=header.invoice_number,
                    invoice_date=header.invoice_date,
                    adjustment_date=combined["date"],
                    description=waiting_label,
                    service=combined["service"],
                    reference=combined["reference"],
                    amount=waiting_amount,
                    source_file=header.source_file,
                )
            )
    else:
        # No order # — this is an invoice-level line. Emit as Adjustment.
        description = order_info or discount_label or waiting_label
        if discount_label and amount == "" and discount_amount:
            amount = discount_amount
        elif waiting_label and amount == "" and waiting_amount:
            amount = waiting_amount
        adjustments.append(
            Adjustment(
                vendor=header.vendor,
                invoice_number=header.invoice_number,
                invoice_date=header.invoice_date,
                adjustment_date=combined["date"],
                description=description or discount_label or waiting_label,
                service=combined["service"],
                reference=combined["reference"],
                amount=amount,
                source_file=header.source_file,
            )
        )
    return orders, adjustments


def _page_line_item_cells(page):
    """Return the list of column-cell-dicts for every visual row in the page's
    line-item table, or [] if the page has no line-item table."""
    table = _find_line_item_table(page)
    if table is None:
        return []
    bounds = _column_bounds(table)
    data_top = table.rows[0].bbox[3]
    data_bottom = table.bbox[3]
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    data_words = [w for w in words if data_top < w["top"] < data_bottom]
    if not data_words:
        return []
    return [
        _assign_to_columns(row, bounds)
        for row in _words_by_row(data_words)
        if any(_assign_to_columns(row, bounds).values())
    ]


def _parse_line_items(pages, header):
    """Stream visual rows from every page through one row-blocking pass so
    orders that wrap across a page break stay together."""
    orders, adjustments = [], []
    current_block = []
    for page in pages:
        for cells in _page_line_item_cells(page):
            if _is_row_start(cells) and current_block:
                o, a = _commit_block(current_block, header)
                orders.extend(o)
                adjustments.extend(a)
                current_block = [cells]
            else:
                current_block.append(cells)
    if current_block:
        o, a = _commit_block(current_block, header)
        orders.extend(o)
        adjustments.extend(a)
    return orders, adjustments


def parse_invoice(pdf_path, vendor):
    """Parse one invoice PDF; returns a ParsedInvoice."""
    source_file = str(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError(f"{pdf_path}: no pages")
        header = _extract_header_fields(pdf.pages[0], vendor, source_file)
        all_orders, all_adjustments = _parse_line_items(pdf.pages, header)
        # Total/Balance Due appear at the bottom-right of the last page, beside
        # the "Total" and "Balance Due" labels. Pull them by word position.
        last = pdf.pages[-1]
        last_words = last.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
        for label in ("Total", "Balance"):
            for w in last_words:
                if w["text"] != label:
                    continue
                # Find the dollar amount on the same visual row, to the right.
                row_amounts = [
                    o for o in last_words
                    if abs(o["top"] - w["top"]) <= 4
                    and o["x0"] > w["x1"]
                    and re.match(r"^\$?[\d,]+\.\d{2}$", o["text"])
                ]
                if row_amounts:
                    amount = row_amounts[-1]["text"].lstrip("$")
                    if label == "Total":
                        header.total = amount
                    else:
                        header.balance_due = amount
                    break
    return ParsedInvoice(header=header, orders=all_orders, adjustments=all_adjustments)


def _build_orders_dir_index(orders_dir):
    """Return a set of order numbers (as strings) present in orders_dir as
    order_<N>.json files. Empty set if the directory does not exist."""
    if not orders_dir or not Path(orders_dir).is_dir():
        return set()
    pattern = re.compile(r"^order_(\d+)\.json$")
    return {
        m.group(1)
        for name in os.listdir(orders_dir)
        if (m := pattern.match(name))
    }


def write_workbook(parsed_invoices, out_path, orders_dir=None):
    """Write three sheets: invoices, orders, adjustments. If orders_dir is
    given, the orders sheet gets a `found_in_orders_dir` column flagging
    which order numbers have a matching order_<N>.json file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fetched_order_ids = _build_orders_dir_index(orders_dir) if orders_dir else None
    workbook = xlsxwriter.Workbook(str(out_path))
    bold = workbook.add_format({"bold": True, "bg_color": "#F2F2F2"})

    inv_sheet = workbook.add_worksheet("invoices")
    inv_cols = (
        "vendor", "invoice_number", "invoice_date", "due_date", "terms",
        "bill_to", "total", "balance_due", "num_orders", "num_adjustments",
        "source_file",
    )
    for c, name in enumerate(inv_cols):
        inv_sheet.write(0, c, name, bold)
    for r, pi in enumerate(parsed_invoices, start=1):
        h = pi.header
        row = (
            h.vendor, h.invoice_number, h.invoice_date, h.due_date, h.terms,
            h.bill_to, h.total, h.balance_due,
            len(pi.orders), len(pi.adjustments),
            h.source_file,
        )
        for c, val in enumerate(row):
            inv_sheet.write(r, c, val)
    inv_sheet.freeze_panes(1, 0)
    inv_sheet.autofilter(0, 0, len(parsed_invoices), len(inv_cols) - 1)

    ord_sheet = workbook.add_worksheet("orders")
    ord_cols = [
        "vendor", "invoice_number", "invoice_date", "order_date",
        "order_number", "from_party", "to_party", "service", "reference",
        "amount", "discount_label", "discount_amount", "raw_order_info",
        "source_file",
    ]
    if fetched_order_ids is not None:
        ord_cols.append("found_in_orders_dir")
    for c, name in enumerate(ord_cols):
        ord_sheet.write(0, c, name, bold)
    r = 1
    for pi in parsed_invoices:
        for o in pi.orders:
            row = [
                o.vendor, o.invoice_number, o.invoice_date, o.order_date,
                o.order_number, o.from_party, o.to_party, o.service, o.reference,
                o.amount, o.discount_label, o.discount_amount, o.raw_order_info,
                o.source_file,
            ]
            if fetched_order_ids is not None:
                row.append("YES" if o.order_number in fetched_order_ids else "NO")
            for c, val in enumerate(row):
                ord_sheet.write(r, c, val)
            r += 1
    ord_sheet.freeze_panes(1, 0)
    if r > 1:
        ord_sheet.autofilter(0, 0, r - 1, len(ord_cols) - 1)

    adj_sheet = workbook.add_worksheet("adjustments")
    adj_cols = (
        "vendor", "invoice_number", "invoice_date", "adjustment_date",
        "description", "service", "reference", "amount", "source_file",
    )
    for c, name in enumerate(adj_cols):
        adj_sheet.write(0, c, name, bold)
    r = 1
    for pi in parsed_invoices:
        for a in pi.adjustments:
            row = (
                a.vendor, a.invoice_number, a.invoice_date, a.adjustment_date,
                a.description, a.service, a.reference, a.amount, a.source_file,
            )
            for c, val in enumerate(row):
                adj_sheet.write(r, c, val)
            r += 1
    adj_sheet.freeze_panes(1, 0)
    if r > 1:
        adj_sheet.autofilter(0, 0, r - 1, len(adj_cols) - 1)

    workbook.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default="input/invoices",
                        help="Root folder containing <vendor>/*.pdf invoices.")
    parser.add_argument("--output", default="output/invoice_orders.xlsx",
                        help="Destination .xlsx path.")
    parser.add_argument("--orders-dir", default="output/orders",
                        help="Directory of order_<N>.json files (from get_orders.py); "
                             "when present, the orders sheet gains a "
                             "found_in_orders_dir column. Pass an empty string to skip.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    in_root = Path(args.input_dir)
    if not in_root.is_dir():
        print(f"input directory not found: {in_root}", file=sys.stderr)
        return 2

    pdfs = list(find_invoice_pdfs(in_root))
    if not pdfs:
        print(f"no PDFs found under {in_root}", file=sys.stderr)
        return 2

    parsed = []
    n_orders = 0
    n_adj = 0
    for vendor, pdf_path in tqdm(pdfs, desc="invoices"):
        try:
            pi = parse_invoice(pdf_path, vendor)
        except Exception as exc:
            LOG.exception("failed to parse %s: %s", pdf_path, exc)
            continue
        parsed.append(pi)
        n_orders += len(pi.orders)
        n_adj += len(pi.adjustments)

    out_path = Path(args.output)
    orders_dir = args.orders_dir or None
    write_workbook(parsed, out_path, orders_dir=orders_dir)

    if orders_dir and Path(orders_dir).is_dir():
        fetched = _build_orders_dir_index(orders_dir)
        found = sum(
            1 for pi in parsed for o in pi.orders
            if o.order_number in fetched
        )
        LOG.info(
            "wrote %s — %d invoices, %d orders (%d found in %s), %d adjustments",
            out_path, len(parsed), n_orders, found, orders_dir, n_adj,
        )
    else:
        LOG.info(
            "wrote %s — %d invoices, %d orders, %d adjustments",
            out_path, len(parsed), n_orders, n_adj,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
