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
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from tqdm import tqdm

BASE_URL = "https://api.dwaybill.com"
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
        except Exception:  # pragma: no cover - logging must never crash the app
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


def fetch_order(cid, order_number, key, customer_number=None, password=None, timeout=30.0):
    """Fetch one order's full detail via GET /{CID}/orders.json/{order_number}.

    Returns the single order dict (unwrapped from the {status, error, body}
    envelope). Use this to check whether the per-order endpoint returns more
    fields than the list endpoint.
    """
    query = urllib.parse.urlencode(_creds(key, customer_number, password))
    url = f"{BASE_URL}/{cid}/orders.json/{order_number}?{query}"
    data, _ = request_json(url, timeout)

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


def highest_saved_order(out_dir):
    """Return the largest order_number already saved in out_dir, or None.

    Derived from the order_<n>.json filenames (no files are opened), so it is
    cheap even for very large archives. Used as the incremental watermark.
    """
    if not os.path.isdir(out_dir):
        return None
    best = None
    for name in os.listdir(out_dir):
        m = re.match(r"order_(\d+)\.json$", name)
        if m:
            n = int(m.group(1))
            best = n if best is None or n > best else best
    return best


def save_order(order, out_dir):
    """Write a single order's complete data to its own JSON file in out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    path = order_path(order, out_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(order, f, indent=2, ensure_ascii=False)
    return path


def get_all_orders(cid, key, customer_number, password, page_size, timeout,
                   max_pages, out_dir=None, page_delay=0.5, skip_terminal=True,
                   max_retries=5, retry_backoff=2.0, incremental=False):
    """Page through /orders.json, saving each order as its own JSON file.

    out_dir is None to skip saving. A page_delay (seconds) pause is inserted
    between consecutive page requests. A tqdm progress bar tracks orders.

    When skip_terminal is True, an order in a terminal state (completed or
    cancelled) is left untouched if its file already exists on disk, since
    such orders are immutable and need not be downloaded/written again.

    Transient request failures are retried (see request_json). If a page still
    fails after max_retries, the run stops gracefully and returns whatever was
    already fetched/saved rather than crashing.
    """
    all_orders = []
    saved = skipped = 0
    total = None
    page_num = 1

    # Incremental: stop paging once we reach an order_number we already have.
    # The list is newest-first, so everything past the watermark is on disk.
    watermark = highest_saved_order(out_dir) if (incremental and out_dir) else None

    logger.info("Fetching orders: page_size=%d, max_pages=%d, page_delay=%.2fs, "
                "skip_terminal=%s, max_retries=%d, out_dir=%s", page_size, max_pages,
                page_delay, skip_terminal, max_retries,
                out_dir if out_dir is not None else "(no save)")
    if incremental:
        if watermark is None:
            logger.info("Incremental mode: no saved orders found — doing a full fetch.")
        else:
            logger.info("Incremental mode: fetching only orders newer than #%d "
                        "(stops when an older/known order is reached).", watermark)

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
            reached_known = False
            for o in orders:
                ident = o.get("order_number") or o.get("id") or "unknown"

                # Incremental watermark: orders are newest-first, so the first
                # one at/below the watermark means everything left is on disk.
                oid = order_id(o)
                if watermark is not None and oid is not None and oid <= watermark:
                    logger.debug("reached known order #%s (<= watermark #%d); "
                                 "stopping pagination", oid, watermark)
                    reached_known = True
                    break

                if out_dir is not None:
                    if (skip_terminal and is_terminal(o)
                            and os.path.exists(order_path(o, out_dir))):
                        skipped += 1
                        page_skipped += 1
                        logger.debug("skip order %s: status=%r is terminal and already "
                                     "saved at %s", ident, o.get("status"),
                                     order_path(o, out_dir))
                    else:
                        save_order(o, out_dir)
                        saved += 1
                        page_saved += 1
                        logger.debug("save order %s: status=%r (%s)", ident,
                                     o.get("status"),
                                     "not terminal" if not is_terminal(o)
                                     else "terminal but not yet saved")
                all_orders.append(o)
                bar.update(1)
            bar.set_postfix(page=page_num, saved=saved, skipped=skipped)
            logger.debug("page %d: got %d order(s) (saved %d, skipped %d); "
                         "running total %d%s", page_num, len(orders), page_saved,
                         page_skipped, len(all_orders),
                         f" of {total}" if total is not None else "")

            # Incremental: we've crossed into already-downloaded territory.
            if reached_known:
                logger.info("Reached already-downloaded orders at page %d; stopping. "
                            "Fetched %d new order(s).", page_num, len(all_orders))
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

    logger.info("Done: retrieved %d order(s); saved %d, skipped %d (terminal, "
                "already downloaded)", len(all_orders), saved, skipped)
    return all_orders, total, saved, skipped


def order_status(o):
    """Derive a readable status from the order's boolean flags."""
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
        print(f"  - #{num}  status={order_status(o)}  customer={cust}  "
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
    parser.add_argument("--out-dir", default="./output/orders",
                        help="Directory for the per-order JSON files (default ./output/orders).")
    parser.add_argument("--no-save", action="store_true",
                        help="Skip writing JSON files; only print the summary.")
    parser.add_argument("--no-skip-terminal", action="store_true",
                        help="Re-download terminal (completed/cancelled) orders even if "
                             "already saved.")
    parser.add_argument("--incremental", action="store_true",
                        help="Fetch only orders newer than the highest already saved, "
                             "stopping pagination as soon as a known order is reached. "
                             "Assumes a complete prior download in --out-dir.")
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

    orders, total, saved, skipped = get_all_orders(
        cid=args.cid,
        key=args.key,
        customer_number=args.customer_number,
        password=args.password,
        page_size=args.page_size,
        timeout=args.timeout,
        max_pages=args.max_pages,
        out_dir=None if args.no_save else args.out_dir,
        page_delay=args.page_delay,
        skip_terminal=not args.no_skip_terminal,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        incremental=args.incremental,
    )

    if args.raw:
        print(json.dumps(orders, indent=2, ensure_ascii=False))
    else:
        summarize(orders)

    if not args.no_save:
        logger.info("Saved %d order file(s) to %s/ (%d terminal order(s) skipped)",
                    saved, args.out_dir, skipped)


if __name__ == "__main__":
    main()
