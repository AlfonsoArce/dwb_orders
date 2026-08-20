#!/usr/bin/env python3
"""Test script for the Digital Waybill API: GET all orders.

Endpoint:  GET https://api.dwaybill.com/{CID}/orders.json
Docs:      https://github.com/digwaybill/Digital-Waybill-API

Credentials are passed as query-string parameters (per the API spec).
Provide them via a .env file, environment variables, or CLI flags
(precedence: CLI flag > environment variable > .env file).

    DWB_CID              company/account id (the {CID} path segment)
    DWB_KEY              API key
    DWB_CUSTOMER_NUMBER  customer number   (only for QuickEntry access)
    DWB_PASSWORD         password          (only for QuickEntry access)

Example .env (see .env.example):
    DWB_CID=your_company_id
    DWB_KEY=your_api_key

Then just run:
    python3 get_orders.py

Every Order fetched is stored in Postgres — that is the source of truth. The
connection string follows the same precedence as the credentials above
(--dsn, then DWB_DSN, then .env), and the run stops immediately if the store
cannot be reached.

Each run also writes the per-order JSON files, because the spreadsheet
exporters still read them, plus an Excel workbook of the fetched orders
(output/orders.xlsx by default). Use --excel PATH to relocate it, --no-excel
to skip it, or --no-json to stop writing the per-order files once the
exporters have been migrated.

A JSON file is only written when its order_number isn't already on disk; see
--overwrite and --refresh-stale to change that. The database has its own rule:
an Order is rewritten only when the dispatch system's revision marker is newer.
"""

import argparse
import collections
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import xlsxwriter
from tqdm import tqdm

from dwb import db, ingest
from dwb.config import load_dotenv
from dwb.fields import DROPPED_STOP_FIELDS, ORDER_FIELDS, STOP_FIELDS

# Overridable so the tests can point the fetcher at a local stub of the API
# and still exercise the real HTTP path: encoding, envelope, paging, retries.
BASE_URL = os.environ.get("DWB_BASE_URL", "https://api.dwaybill.com")
API_VERSION = "1"
# Terminal statuses: orders in these states are immutable, so once saved we
# don't download them again. Both UK/US spellings of cancelled are covered.
TERMINAL_STATUSES = {"completed", "cancelled", "canceled"}
# Transient HTTP statuses worth retrying (rate limit / server overload).
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF = 60.0  # cap for a single backoff wait, in seconds

logger = logging.getLogger("dwb_orders")


class APIRequestError(Exception):
    """A request failed transiently even after exhausting retries.

    Distinct from a fatal error (bad credentials, malformed response): the
    caller may stop gracefully and keep whatever was already fetched.
    """

# Redact secrets (key=..., password=...) from any text before logging it.
_SECRET_RE = re.compile(r"((?:key|password)=)[^&\s]+", re.IGNORECASE)


def redact(text):
    """Replace key=/password= query values with *** for safe logging."""
    return _SECRET_RE.sub(r"\1***", str(text))


class TqdmLoggingHandler(logging.Handler):
    """Emit log records via tqdm.write so they don't corrupt the progress bar."""

    def emit(self, record):
        try:
            tqdm.write(self.format(record), file=sys.stderr)
            self.flush()
        except Exception:  # pragma: no cover - a handler must never crash the app  # noqa: BLE001
            self.handleError(record)


def setup_logging(level="INFO", log_file=None, console_level=None):
    """Configure the module logger with a tqdm-safe console handler and an
    optional file handler.

    level controls the file (and the default console) verbosity. Pass
    console_level to set the console independently — e.g. "ERROR" to keep the
    console to just the progress bar while the file captures full DEBUG logs.
    Returns the configured logger.
    """
    file_level = getattr(logging, str(level).upper(), logging.INFO)
    console_level = getattr(logging, str(console_level or level).upper(), logging.INFO)
    logger.setLevel(logging.DEBUG)  # handlers do the filtering
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    console = TqdmLoggingHandler()
    console.setLevel(console_level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(file_level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.info("Logging to file: %s", log_file)

    return logger


def decode_body(raw, resp_headers=None):
    """Decode bytes using the response charset, falling back to cp1252.

    The Digital Waybill API returns Windows-1252 encoded text, so a plain
    utf-8 decode raises on bytes like 0x94 (curly quotes).
    """
    charset = None
    if resp_headers is not None and hasattr(resp_headers, "get_content_charset"):
        charset = resp_headers.get_content_charset()
    for enc in (charset, "utf-8", "cp1252", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _retry_after_seconds(headers, default):
    """Return the Retry-After delay (integer seconds form) or `default`."""
    if headers is None:
        return default
    val = headers.get("Retry-After")
    try:
        return max(0.0, float(val)) if val else default
    except (TypeError, ValueError):
        return default  # HTTP-date form not parsed; fall back to backoff


def request_json(url, timeout, max_retries=5, backoff=2.0):
    """GET a URL and return (parsed_json, url).

    Transient failures (HTTP 429/5xx, connection errors, timeouts) are retried
    up to `max_retries` times with exponential backoff (honoring Retry-After),
    capped at MAX_BACKOFF per wait. Exhausted transient retries raise
    APIRequestError so the caller can stop gracefully and keep partial results.
    Non-retryable errors (e.g. 401/404) or malformed JSON abort the program.
    """
    safe_url = redact(url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    attempt = 0
    while True:
        attempt += 1
        logger.debug("GET %s (attempt %d/%d)", safe_url, attempt, max_retries + 1)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(decode_body(resp.read(), resp.headers)), url
        except urllib.error.HTTPError as e:
            detail = decode_body(e.read(), getattr(e, "headers", None))
            if e.code in RETRYABLE_STATUS and attempt <= max_retries:
                wait = min(_retry_after_seconds(getattr(e, "headers", None),
                                                backoff * 2 ** (attempt - 1)), MAX_BACKOFF)
                logger.warning("HTTP %s %s for %s — retrying in %.1fs (attempt %d/%d)",
                               e.code, e.reason, safe_url, wait, attempt, max_retries + 1)
                time.sleep(wait)
                continue
            if e.code in RETRYABLE_STATUS:
                logger.error("HTTP %s %s for %s — giving up after %d attempt(s)\n%s",
                             e.code, e.reason, safe_url, attempt, redact(detail))
                raise APIRequestError(f"HTTP {e.code} after {attempt} attempts")
            logger.error("HTTP %s %s for %s\n%s", e.code, e.reason, safe_url, redact(detail))
            raise SystemExit(1)
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            if attempt <= max_retries:
                wait = min(backoff * 2 ** (attempt - 1), MAX_BACKOFF)
                logger.warning("Connection error for %s: %s — retrying in %.1fs "
                               "(attempt %d/%d)", safe_url, reason, wait,
                               attempt, max_retries + 1)
                time.sleep(wait)
                continue
            logger.error("Connection error for %s: %s — giving up after %d attempt(s)",
                         safe_url, reason, attempt)
            raise APIRequestError(f"Connection error after {attempt} attempts")
        except json.JSONDecodeError as e:
            logger.error("Response was not valid JSON from %s: %s", safe_url, e)
            raise SystemExit(1)


def _creds(key, customer_number, password, extra=None):
    """Build the common credential query params, dropping any that are None."""
    params = {"v": API_VERSION, "key": key,
              "customer_number": customer_number, "password": password}
    if extra:
        params.update(extra)
    return {k: v for k, v in params.items() if v is not None}


def fetch_page(cid, params, timeout, max_retries=5, backoff=2.0):
    """Fetch a single page of orders and return the parsed JSON dict."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE_URL}/{cid}/orders.json?{query}"
    return request_json(url, timeout, max_retries=max_retries, backoff=backoff)


def fetch_order(cid, order_number, key, customer_number=None, password=None,
                timeout=30.0, max_retries=5, backoff=2.0):
    """Fetch one order's full detail via GET /{CID}/orders.json/{order_number}.

    Returns the single order dict (unwrapped from the {status, error, body}
    envelope). Use this to check whether the per-order endpoint returns more
    fields than the list endpoint.
    """
    query = urllib.parse.urlencode(_creds(key, customer_number, password))
    url = f"{BASE_URL}/{cid}/orders.json/{order_number}?{query}"
    data, _ = request_json(url, timeout, max_retries=max_retries, backoff=backoff)

    if isinstance(data, dict) and data.get("error"):
        logger.error("API error (status %s): %s", data.get("status"), data["error"])
        raise SystemExit(1)

    body = data.get("body", data) if isinstance(data, dict) else data
    # body may be the order itself, or wrap it under "order"/"orders".
    if isinstance(body, dict):
        if "order" in body and isinstance(body["order"], dict):
            return body["order"]
        if "orders" in body and isinstance(body["orders"], list):
            return body["orders"][0] if body["orders"] else {}
    return body


def order_path(order, out_dir):
    """Return the on-disk JSON path for an order."""
    ident = order.get("order_number") or order.get("id") or "unknown"
    return os.path.join(out_dir, f"order_{ident}.json")


def is_terminal(order):
    """True if the order's status is terminal (completed or cancelled)."""
    return str(order.get("status", "")).strip().lower() in TERMINAL_STATUSES


def order_id(order):
    """Return the order_number as an int, or None if it isn't numeric."""
    try:
        return int(order.get("order_number"))
    except (TypeError, ValueError):
        return None


def saved_is_terminal(path):
    """True if the order already on disk is in a terminal state.

    Only the saved copy's status is read. An unreadable file counts as
    non-terminal so it gets rewritten rather than left corrupt.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return is_terminal(json.load(fh))
    except (OSError, json.JSONDecodeError):
        return False


def save_decision(path, known, overwrite=False, refresh_stale=False):
    """Decide whether an order needs writing; return (save?, reason).

    Default policy: an order whose file already exists is never rewritten. The
    archive is the source of truth and a redundant write is pure cost — in a
    synced folder (iCloud/Dropbox) it also means another sync round-trip and a
    chance of a conflict copy.

    overwrite rewrites everything. refresh_stale rewrites only those whose
    saved copy is still in flight, which is how a copy captured while the order
    was open eventually picks up its completed state.
    """
    if not known:
        return True, "new"
    if overwrite:
        return True, "overwrite requested"
    if refresh_stale and not saved_is_terminal(path):
        return True, "saved copy not terminal"
    return False, "already saved"


def save_order(order, out_dir):
    """Write a single order's complete data to its own JSON file in out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    path = order_path(order, out_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(order, f, indent=2, ensure_ascii=False)
    return path


FetchResult = collections.namedtuple(
    "FetchResult", "orders total saved skipped orders_stored stops_stored")

SweepResult = collections.namedtuple(
    "SweepResult", "considered requested orders_stored stops_stored")


def sql_watermark(conn, source_key=ingest.DEFAULT_SOURCE):
    """Return the highest Order Number stored for this Source, or None.

    This is where an incremental run resumes from. It used to come from
    listing 169,000 filenames; one query is cheaper, and correct even when the
    sync daemon is halfway through re-enumerating the folder.
    """
    with conn.cursor() as cur:
        cur.execute("select max(order_number) from orders where source_key = %s",
                    (source_key,))
        return cur.fetchone()[0]


def in_flight_order_numbers(conn, limit, source_key=ingest.DEFAULT_SOURCE):
    """Return Order Numbers not yet in a Terminal Status, newest first.

    Bounded by `limit`: each one costs a request against a rate-limited API,
    so an unbounded sweep of a large backlog could run for hours.
    """
    with conn.cursor() as cur:
        cur.execute("select order_number from orders "
                    "where source_key = %s and not is_terminal "
                    "order by order_number desc limit %s", (source_key, limit))
        return [row[0] for row in cur.fetchall()]


def _store(conn, orders, source_key):
    """Write a page's worth of Orders and commit, so a stopped run keeps them."""
    if conn is None or not orders:
        return 0, 0
    result = ingest.ingest_orders(conn, orders, source_key)
    conn.commit()
    return result.orders_written, result.stops_written


def get_all_orders(cid, key, customer_number, password, page_size, timeout,
                   max_pages, out_dir=None, page_delay=0.5, overwrite=False,
                   refresh_stale=False, max_retries=5, retry_backoff=2.0,
                   incremental=False, conn=None, overlap_pages=0,
                   source_key=ingest.DEFAULT_SOURCE):
    """Page through /orders.json, storing each Order and saving it as a file.

    Every Order retrieved is written to the database (conn), one commit per
    page, so an interrupted run keeps what it already had. It is also written
    to out_dir as JSON unless out_dir is None — the spreadsheet exporters still
    read those files, and stopping would quietly shrink their reports.

    Incremental runs resume from the highest Order Number in the database, but
    always re-request the first `overlap_pages` pages regardless. Without that,
    an Order fetched while In Flight would keep that status for ever: the
    watermark means it is never looked at again. The revision guard throws away
    everything that has not actually changed, so the overlap costs requests and
    no writes.

    Transient request failures are retried (see request_json). If a page still
    fails after max_retries, the run stops gracefully and returns whatever was
    already fetched rather than crashing.
    """
    all_orders = []
    saved = skipped = 0
    orders_stored = stops_stored = 0
    total = None
    page_num = 1

    watermark = sql_watermark(conn, source_key) if (incremental and conn) else None

    policy = ("overwrite everything" if overwrite else
              "skip already-saved orders" + (", refresh non-terminal copies"
                                             if refresh_stale else ""))
    logger.info("Fetching orders: page_size=%d, max_pages=%d, page_delay=%.2fs, "
                "policy=%s, max_retries=%d, out_dir=%s", page_size, max_pages,
                page_delay, policy, max_retries,
                out_dir if out_dir is not None else "(no files)")
    if incremental:
        if watermark is None:
            logger.info("Incremental mode: no Orders stored yet — doing a full fetch.")
        else:
            logger.info("Incremental mode: resuming after Order #%d, after "
                        "re-requesting the first %d page(s) to catch Orders "
                        "whose status has moved on.", watermark, overlap_pages)

    # Until the first response tells us the real count, bound the bar by what
    # the page settings could return; we shrink it to the true total below.
    bar = tqdm(total=max_pages * page_size, unit="order", desc="Orders")

    try:
        while page_num <= max_pages:
            params = {
                "v": API_VERSION,
                "key": key,
                "customer_number": customer_number,
                "password": password,
                "page_size": page_size,
                "page_num": page_num,
            }
            try:
                data, _ = fetch_page(cid, params, timeout,
                                     max_retries=max_retries, backoff=retry_backoff)
            except APIRequestError as e:
                logger.error("Stopping at page %d after repeated failures (%s). "
                             "Keeping %d order(s) already fetched; rerun to resume.",
                             page_num, e, len(all_orders))
                break

            # The API wraps results in an envelope: {status, error, body:{...}}.
            if isinstance(data, dict) and data.get("error"):
                logger.error("API error (status %s): %s",
                             data.get("status"), data["error"])
                raise SystemExit(1)
            body = data.get("body", data) if isinstance(data, dict) else {}

            orders = body.get("orders", []) if isinstance(body, dict) else []
            total = body.get("count") if isinstance(body, dict) else None
            # The server may clamp page_size below what we requested (it caps at
            # 50), so use its reported value to detect a "full" (non-last) page.
            effective_page_size = body.get("page_size") or page_size

            # Now that we know the real total, cap the progress bar at it.
            if total is not None:
                bar.total = min(total, max_pages * effective_page_size)
                bar.refresh()

            page_saved = page_skipped = 0
            page_orders = []
            reached_known = False
            # The overlap pages are re-read in full; the watermark only applies
            # once we are past them.
            watermark_applies = watermark is not None and page_num > overlap_pages
            for o in orders:
                ident = o.get("order_number") or o.get("id") or "unknown"

                # Orders come back newest-first, so the first one at or below
                # the watermark means everything left is already stored.
                oid = order_id(o)
                if watermark_applies and oid is not None and oid <= watermark:
                    logger.debug("reached known order #%s (<= watermark #%d); "
                                 "stopping pagination", oid, watermark)
                    reached_known = True
                    break

                page_orders.append(o)

                if out_dir is not None:
                    path = order_path(o, out_dir)
                    known = os.path.exists(path)
                    do_save, reason = save_decision(path, known, overwrite=overwrite,
                                                    refresh_stale=refresh_stale)
                    if do_save:
                        save_order(o, out_dir)
                        saved += 1
                        page_saved += 1
                        logger.debug("save order %s (%s): status=%r", ident, reason,
                                     o.get("status"))
                    else:
                        skipped += 1
                        page_skipped += 1
                        logger.debug("skip order %s (%s): status=%r", ident, reason,
                                     o.get("status"))
                all_orders.append(o)
                bar.update(1)

            stored, stops = _store(conn, page_orders, source_key)
            orders_stored += stored
            stops_stored += stops

            bar.set_postfix(page=page_num, stored=orders_stored, saved=saved)
            logger.debug("page %d: got %d order(s) (stored %d, wrote %d file(s), "
                         "skipped %d); running total %d%s", page_num, len(orders),
                         stored, page_saved, page_skipped, len(all_orders),
                         f" of {total}" if total is not None else "")

            # Incremental: we've crossed into already-stored territory.
            if reached_known:
                logger.info("Reached already-stored Orders at page %d; stopping. "
                            "Fetched %d order(s).", page_num, len(all_orders))
                break

            # Stop when this page wasn't full, or we've reached the reported count.
            if not orders or len(orders) < effective_page_size:
                break
            if total is not None and len(all_orders) >= total:
                break

            time.sleep(page_delay)  # pause between page requests
            page_num += 1
    finally:
        bar.close()

    logger.info("Done: retrieved %d order(s); stored %d, wrote %d file(s), "
                "skipped %d (already saved)", len(all_orders), orders_stored,
                saved, skipped)
    return FetchResult(all_orders, total, saved, skipped, orders_stored, stops_stored)


def sweep_in_flight(conn, cid, key, customer_number=None, password=None,
                    timeout=30.0, limit=200, preview=False, request_delay=1.0,
                    max_retries=5, retry_backoff=2.0, out_dir=None,
                    overwrite=False, refresh_stale=False,
                    source_key=ingest.DEFAULT_SOURCE):
    """Re-request the Orders that are still In Flight, one at a time.

    Paging overlap catches almost all of them far more cheaply — fifty Orders
    per request against one. This is for the straggler that stalled long enough
    to fall outside the overlap window, so it is opt-in and bounded.
    """
    numbers = in_flight_order_numbers(conn, limit, source_key)
    if not numbers:
        logger.info("No Orders are In Flight; nothing to sweep.")
        return SweepResult(0, 0, 0, 0)

    if preview:
        logger.info("Would re-request %d In Flight Order(s): %s", len(numbers),
                    ", ".join(f"#{n}" for n in numbers[:20])
                    + (" ..." if len(numbers) > 20 else ""))
        return SweepResult(len(numbers), 0, 0, 0)

    logger.info("Sweeping %d In Flight Order(s), one request each.", len(numbers))
    requested = orders_stored = stops_stored = 0
    bar = tqdm(total=len(numbers), unit="order", desc="Sweep")
    try:
        for number in numbers:
            try:
                order = fetch_order(cid, number, key, customer_number, password,
                                    timeout=timeout, max_retries=max_retries,
                                    backoff=retry_backoff)
            except APIRequestError as e:
                logger.error("Stopping the sweep after repeated failures (%s). "
                             "%d Order(s) re-requested so far; rerun to continue.",
                             e, requested)
                break
            requested += 1
            if not order:
                logger.warning("Order #%s returned nothing; leaving it as it is.",
                               number)
                bar.update(1)
                continue
            if out_dir is not None:
                path = order_path(order, out_dir)
                do_save, _reason = save_decision(path, os.path.exists(path),
                                                 overwrite=overwrite,
                                                 refresh_stale=refresh_stale)
                if do_save:
                    save_order(order, out_dir)
            stored, stops = _store(conn, [order], source_key)
            orders_stored += stored
            stops_stored += stops
            bar.update(1)
            time.sleep(request_delay)
    finally:
        bar.close()

    logger.info("Sweep done: re-requested %d Order(s), updated %d.",
                requested, orders_stored)
    return SweepResult(len(numbers), requested, orders_stored, stops_stored)


# ---------------------------------------------------------------------------
# Excel workbook output
# ---------------------------------------------------------------------------

# Excel cells max out at 32,767 chars; longer text is truncated with a marker
# so a single outlier row can't abort the whole write.
EXCEL_CELL_LIMIT = 32_767
TRUNC_MARKER = "…[truncated]"

# A worksheet holds 1,048,576 rows; one of ours is the header. xlsxwriter
# silently drops out-of-range writes, so we stop and warn instead.
EXCEL_MAX_DATA_ROWS = 1_048_575

# Order-level sheet: raw fields (see dwb.fields) plus a few derived columns so
# the sheet is usable on its own (first stop is the pickup, last stop the
# final delivery).
ORDER_COLUMNS = list(ORDER_FIELDS) + [
    "flag_status",
    "stop_count",
    "first_stop_company",
    "first_stop_city",
    "last_stop_company",
    "last_stop_city",
]

STOP_COLUMNS = (
    ["order_number", "stop_index", "stop_count"]
    + [k for k in STOP_FIELDS if k not in DROPPED_STOP_FIELDS]
    + ["contact_name", "contact_phone", "packages_json"]
)


def cell_value(v):
    """Coerce a JSON value into something xlsxwriter can write to a cell."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if not isinstance(v, str):
        # Lists/dicts with no dedicated column get JSON-serialized as a fallback.
        v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if len(v) > EXCEL_CELL_LIMIT:
        return v[: EXCEL_CELL_LIMIT - len(TRUNC_MARKER)] + TRUNC_MARKER
    return v


def order_row(order):
    """Build the "orders" sheet row for one order."""
    stops = order.get("route_stops") or []
    first = stops[0] if stops else {}
    last = stops[-1] if stops else {}
    row = [cell_value(order.get(k)) for k in ORDER_FIELDS]
    row += [
        order_flags(order),
        len(stops),
        cell_value(first.get("company")),
        cell_value(first.get("city")),
        cell_value(last.get("company")),
        cell_value(last.get("city")),
    ]
    return row


def stop_rows(order):
    """Yield one "route_stops" sheet row per stop, prefixed with order context."""
    stops = order.get("route_stops") or []
    stop_count = len(stops)
    for i, stop in enumerate(stops, start=1):
        if not isinstance(stop, dict):
            continue
        contact = stop.get("contact") or {}
        packages = stop.get("packages")
        yield (
            [cell_value(order.get("order_number")), i, stop_count]
            + [cell_value(stop.get(k)) for k in STOP_FIELDS if k not in DROPPED_STOP_FIELDS]
            + [
                cell_value(contact.get("name")),
                cell_value(contact.get("phone")),
                cell_value(packages) if packages is not None else "",
            ]
        )


def write_workbook(orders, path, with_stops=True):
    """Write `orders` to an .xlsx workbook and return (order_rows, stop_rows).

    Sheet "orders" holds one row per order; sheet "route_stops" (unless
    with_stops is False) holds one row per stop, keyed by order_number.

    constant_memory streams rows straight to disk, which keeps large archives
    from ballooning in RAM — the trade-off is that rows must be written in order
    and a finished sheet can't be revisited, hence the two sequential passes.
    """
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    book = xlsxwriter.Workbook(path, {"constant_memory": True, "strings_to_urls": False})
    bold = book.add_format({"bold": True})
    n_orders = n_stops = 0
    try:
        sheet = book.add_worksheet("orders")
        sheet.write_row(0, 0, ORDER_COLUMNS, bold)
        sheet.freeze_panes(1, 0)
        dropped_orders = dropped_stops = 0
        for o in orders:
            if not isinstance(o, dict):
                continue
            if n_orders >= EXCEL_MAX_DATA_ROWS:
                dropped_orders += 1
                continue
            n_orders += 1
            sheet.write_row(n_orders, 0, order_row(o))
        if dropped_orders:
            logger.warning("orders sheet hit Excel's %d-row limit; %d order(s) omitted",
                           EXCEL_MAX_DATA_ROWS, dropped_orders)

        if with_stops:
            stops_sheet = book.add_worksheet("route_stops")
            stops_sheet.write_row(0, 0, STOP_COLUMNS, bold)
            stops_sheet.freeze_panes(1, 0)
            for o in orders:
                if not isinstance(o, dict):
                    continue
                for row in stop_rows(o):
                    if n_stops >= EXCEL_MAX_DATA_ROWS:
                        dropped_stops += 1
                        continue
                    n_stops += 1
                    stops_sheet.write_row(n_stops, 0, row)
            if dropped_stops:
                logger.warning("route_stops sheet hit Excel's %d-row limit; %d stop(s) "
                               "omitted — use process_route_stops.py for the full set",
                               EXCEL_MAX_DATA_ROWS, dropped_stops)
    finally:
        book.close()

    return n_orders, n_stops


def load_saved_orders(out_dir):
    """Load every order_<n>.json in out_dir, sorted by order_number.

    Used by --excel-from-dir so the workbook can cover the whole archive on
    disk, not just the orders fetched in this run (which is nearly empty under
    --incremental).
    """
    if not os.path.isdir(out_dir):
        logger.error("Cannot build workbook: directory not found: %s", out_dir)
        return []
    names = []
    with os.scandir(out_dir) as it:
        for e in it:
            m = re.match(r"order_(\d+)\.json$", e.name)
            if m and e.is_file():
                names.append((int(m.group(1)), e.name))
    names.sort()

    orders = []
    failed = 0
    for _, name in tqdm(names, desc="Reading saved orders", unit="order"):
        try:
            with open(os.path.join(out_dir, name), "r", encoding="utf-8") as fh:
                orders.append(json.load(fh))
        except (OSError, json.JSONDecodeError) as e:
            failed += 1
            logger.warning("Skipping %s: %s", name, e)
    if failed:
        logger.warning("%d saved order file(s) could not be read", failed)
    logger.info("Loaded %d saved order(s) from %s", len(orders), out_dir)
    return orders


def order_flags(o):
    """Return the Order Flags — the read-state marker dispatch staff set.

    pending / flagged / read describe whether a human has looked at the Order.
    This is not Order Status: an Order can be flagged and Completed at once.
    """
    if o.get("pending"):
        return "pending"
    if o.get("flagged"):
        return "flagged"
    return "read" if o.get("read") else "open"


def summarize(orders):
    if not orders:
        print("\nNo orders returned.")
        return
    print(f"\nRetrieved {len(orders)} order(s). Sample:")
    for o in orders[:5]:
        num = o.get("order_number", "?")
        cust = o.get("customer_number", "?")
        ready = o.get("ready_time", "")
        price = o.get("final_price", o.get("price", ""))
        print(f"  - #{num}  flags={order_flags(o)}  customer={cust}  "
              f"ready={ready}  price={price}")
    if len(orders) > 5:
        print(f"  ... and {len(orders) - 5} more")


def main():
    load_dotenv()  # populate os.environ from .env before reading defaults

    parser = argparse.ArgumentParser(description="Test Digital Waybill GET all orders.")
    parser.add_argument("--cid", default=os.environ.get("DWB_CID"),
                        help="Company/account id (path segment). Env: DWB_CID")
    parser.add_argument("--key", default=os.environ.get("DWB_KEY"),
                        help="API key. Env: DWB_KEY")
    parser.add_argument("--customer-number", default=os.environ.get("DWB_CUSTOMER_NUMBER"),
                        help="Customer number (QuickEntry). Env: DWB_CUSTOMER_NUMBER")
    parser.add_argument("--password", default=os.environ.get("DWB_PASSWORD"),
                        help="Password (QuickEntry). Env: DWB_PASSWORD")
    parser.add_argument("--page-size", type=int, default=50,
                        help="Orders per page (default 50; the API caps this at 50).")
    parser.add_argument("--max-pages", type=int, default=1,
                        help="Safety cap on pages (default 1; raise to fetch more).")
    parser.add_argument("--page-delay", type=float, default=1.0,
                        help="Seconds to wait between page requests (default 1.0).")
    parser.add_argument("--max-retries", type=int, default=5,
                        help="Retries for transient errors (429/5xx/network) per "
                             "request (default 5).")
    parser.add_argument("--retry-backoff", type=float, default=2.0,
                        help="Base seconds for exponential retry backoff (default 2.0).")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout (s).")
    parser.add_argument("--dsn", default=None,
                        help="Postgres connection string for the Order store. "
                             "Env: DWB_DSN, or a DWB_DSN line in .env.")
    parser.add_argument("--out-dir", default="./output/orders",
                        help="Directory for the per-order JSON files (default ./output/orders).")
    parser.add_argument("--no-json", "--no-save", dest="no_json", action="store_true",
                        help="Stop writing the per-order JSON files. Orders still go "
                             "to the database. The spreadsheet exporters still read "
                             "those files, so leave this off until they are migrated.")
    parser.add_argument("--overwrite", "--no-skip-terminal", action="store_true",
                        help="Re-save every fetched order, even one already on disk "
                             "(default: an order whose file exists is never rewritten).")
    parser.add_argument("--refresh-stale", action="store_true",
                        help="Also re-save an already-saved order whose stored copy is "
                             "not yet completed/cancelled, so a copy captured while the "
                             "order was still in flight picks up its final state.")
    parser.add_argument("--incremental", action="store_true",
                        help="Fetch only Orders newer than the highest one stored, "
                             "stopping pagination as soon as a known Order is reached. "
                             "Where to resume comes from the database, not the folder.")
    parser.add_argument("--overlap-pages", type=int, default=5, metavar="N",
                        help="Re-request the N most recent pages even under "
                             "--incremental (default 5, i.e. 250 Orders). This is how "
                             "an Order captured while In Flight picks up its final "
                             "status; unchanged Orders cost no writes.")
    parser.add_argument("--refresh-in-flight", action="store_true",
                        help="After fetching, re-request the Orders still In Flight "
                             "individually — the stragglers that fell outside the "
                             "overlap window. One request each, so it is opt-in.")
    parser.add_argument("--sweep-limit", type=int, default=200, metavar="N",
                        help="Most Orders one --refresh-in-flight sweep will request "
                             "(default 200). Newest first.")
    parser.add_argument("--sweep-preview", action="store_true",
                        help="Report which Orders --refresh-in-flight would request, "
                             "and request none of them.")
    parser.add_argument("--excel", default="output/orders.xlsx", metavar="PATH",
                        help="Excel workbook to write: an 'orders' sheet (one row per order) "
                             "and a 'route_stops' sheet (one row per stop) "
                             "(default output/orders.xlsx).")
    parser.add_argument("--no-excel", action="store_true",
                        help="Skip writing the Excel workbook.")
    parser.add_argument("--excel-from-dir", action="store_true",
                        help="Build the workbook from every order_*.json in --out-dir (the "
                             "whole archive) instead of only the orders fetched this run. "
                             "Use this with --incremental.")
    parser.add_argument("--no-excel-stops", action="store_true",
                        help="Omit the route_stops sheet from the workbook.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log verbosity for the file, and console default (default INFO).")
    parser.add_argument("--console-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Console verbosity, separate from the file. Use ERROR to show "
                             "only the progress bar (default: same as --log-level).")
    parser.add_argument("--log-file", default=None,
                        help="Also write logs to this file (e.g. logs/fetch.log).")
    parser.add_argument("--raw", action="store_true", help="Print full JSON of all orders.")
    args = parser.parse_args()

    setup_logging(level=args.log_level, log_file=args.log_file,
                  console_level=args.console_level)

    missing = [n for n, v in (("--cid/DWB_CID", args.cid), ("--key/DWB_KEY", args.key)) if not v]
    if missing:
        parser.error(f"Missing required credential(s): {', '.join(missing)}")

    if args.excel_from_dir and args.no_excel:
        parser.error("--excel-from-dir has no effect with --no-excel")
    if args.excel_from_dir and args.no_json:
        parser.error("--excel-from-dir reads the saved order files, so it can't be "
                     "combined with --no-json")

    # Connect before the first request: a run that fetches for twenty minutes
    # and then discovers it cannot store anything has wasted rate limit.
    try:
        conn = db.connect(args.dsn)
    except db.DatabaseUnavailable as e:
        logger.error("%s", e)
        return 2
    logger.info("Order store: %s", db.safe_dsn(db.resolve_dsn(args.dsn)))

    out_dir = None if args.no_json else args.out_dir
    try:
        result = get_all_orders(
            cid=args.cid,
            key=args.key,
            customer_number=args.customer_number,
            password=args.password,
            page_size=args.page_size,
            timeout=args.timeout,
            max_pages=args.max_pages,
            out_dir=out_dir,
            page_delay=args.page_delay,
            overwrite=args.overwrite,
            refresh_stale=args.refresh_stale,
            max_retries=args.max_retries,
            retry_backoff=args.retry_backoff,
            incremental=args.incremental,
            conn=conn,
            overlap_pages=max(0, args.overlap_pages),
        )
        orders, saved, skipped = result.orders, result.saved, result.skipped

        if args.refresh_in_flight or args.sweep_preview:
            sweep_in_flight(
                conn, args.cid, args.key, args.customer_number, args.password,
                timeout=args.timeout, limit=args.sweep_limit,
                preview=args.sweep_preview, request_delay=args.page_delay,
                max_retries=args.max_retries, retry_backoff=args.retry_backoff,
                out_dir=out_dir, overwrite=args.overwrite,
                refresh_stale=args.refresh_stale)
    finally:
        conn.close()

    if args.raw:
        print(json.dumps(orders, indent=2, ensure_ascii=False))
    else:
        summarize(orders)

    logger.info("Stored %d Order(s) and %d Route Stop(s) in the database",
                result.orders_stored, result.stops_stored)
    if not args.no_json:
        logger.info("Wrote %d order file(s) to %s/ (%d already-saved order(s) skipped)",
                    saved, args.out_dir, skipped)

    if not args.no_excel:
        source = load_saved_orders(args.out_dir) if args.excel_from_dir else orders
        if not source:
            logger.warning("No orders to export — writing an empty workbook to %s",
                           args.excel)
        n_orders, n_stops = write_workbook(source, args.excel,
                                           with_stops=not args.no_excel_stops)
        logger.info("Excel workbook: %s (%d order row(s), %d stop row(s))",
                    os.path.abspath(args.excel), n_orders, n_stops)
    return 0


if __name__ == "__main__":
    sys.exit(main())
