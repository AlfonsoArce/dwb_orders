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
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.dwaybill.com"
API_VERSION = "1"


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


def request_json(url, timeout):
    """GET a URL and return (parsed_json, url), handling decode/HTTP errors."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = decode_body(resp.read(), resp.headers)
            return json.loads(body), url
    except urllib.error.HTTPError as e:
        detail = decode_body(e.read(), getattr(e, "headers", None))
        raise SystemExit(f"HTTP {e.code} {e.reason} for {url}\n{detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Connection error for {url}: {e.reason}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"Response was not valid JSON: {e}")


def _creds(key, customer_number, password, extra=None):
    """Build the common credential query params, dropping any that are None."""
    params = {"v": API_VERSION, "key": key,
              "customer_number": customer_number, "password": password}
    if extra:
        params.update(extra)
    return {k: v for k, v in params.items() if v is not None}


def fetch_page(cid, params, timeout):
    """Fetch a single page of orders and return the parsed JSON dict."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE_URL}/{cid}/orders.json?{query}"
    return request_json(url, timeout)


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
        raise SystemExit(f"API error (status {data.get('status')}): {data['error']}")

    body = data.get("body", data) if isinstance(data, dict) else data
    # body may be the order itself, or wrap it under "order"/"orders".
    if isinstance(body, dict):
        if "order" in body and isinstance(body["order"], dict):
            return body["order"]
        if "orders" in body and isinstance(body["orders"], list):
            return body["orders"][0] if body["orders"] else {}
    return body


def save_order(order, out_dir):
    """Write a single order's complete data to its own JSON file in out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    ident = order.get("order_number") or order.get("id") or "unknown"
    path = os.path.join(out_dir, f"order_{ident}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(order, f, indent=2, ensure_ascii=False)
    return path


def get_all_orders(cid, key, customer_number, password, page_size, timeout,
                   max_pages, out_dir=None, page_delay=0.5):
    """Page through /orders.json, saving each order as its own JSON file.

    out_dir is None to skip saving. A page_delay (seconds) pause is inserted
    between consecutive page requests.
    """
    all_orders = []
    saved = 0
    total = None
    page_num = 1

    while page_num <= max_pages:
        params = {
            "v": API_VERSION,
            "key": key,
            "customer_number": customer_number,
            "password": password,
            "page_size": page_size,
            "page_num": page_num,
        }
        data, url = fetch_page(cid, params, timeout)

        # Mask the key when echoing the URL we called.
        print(f"-> GET {url.replace(key, key[:4] + '...') if key else url}")

        # The API wraps results in an envelope: {status, error, body:{...}}.
        if isinstance(data, dict) and data.get("error"):
            raise SystemExit(f"API error (status {data.get('status')}): {data['error']}")
        body = data.get("body", data) if isinstance(data, dict) else {}

        orders = body.get("orders", []) if isinstance(body, dict) else []
        total = body.get("count") if isinstance(body, dict) else None
        all_orders.extend(orders)

        # Save each order from this page immediately (one file per order).
        if out_dir is not None:
            for o in orders:
                save_order(o, out_dir)
                saved += 1

        print(f"   page {page_num}: got {len(orders)} order(s); "
              f"running total {len(all_orders)}"
              + (f" of {total}" if total is not None else "")
              + (f"; saved {saved} file(s)" if out_dir is not None else ""))

        # Stop when this page wasn't full, or we've reached the reported count.
        if not orders or len(orders) < page_size:
            break
        if total is not None and len(all_orders) >= total:
            break

        time.sleep(page_delay)  # pause between page requests
        page_num += 1

    return all_orders, total, saved


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
    parser.add_argument("--page-size", type=int, default=5,
                        help="Orders per page (default 5).")
    parser.add_argument("--max-pages", type=int, default=1,
                        help="Safety cap on pages (default 1; raise to fetch more).")
    parser.add_argument("--page-delay", type=float, default=0.5,
                        help="Seconds to wait between page requests (default 0.5).")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout (s).")
    parser.add_argument("--out-dir", default="./output",
                        help="Directory for the per-order JSON files (default ./output).")
    parser.add_argument("--no-save", action="store_true",
                        help="Skip writing JSON files; only print the summary.")
    parser.add_argument("--raw", action="store_true", help="Print full JSON of all orders.")
    args = parser.parse_args()

    missing = [n for n, v in (("--cid/DWB_CID", args.cid), ("--key/DWB_KEY", args.key)) if not v]
    if missing:
        parser.error(f"Missing required credential(s): {', '.join(missing)}")

    orders, total, saved = get_all_orders(
        cid=args.cid,
        key=args.key,
        customer_number=args.customer_number,
        password=args.password,
        page_size=args.page_size,
        timeout=args.timeout,
        max_pages=args.max_pages,
        out_dir=None if args.no_save else args.out_dir,
        page_delay=args.page_delay,
    )

    if args.raw:
        print(json.dumps(orders, indent=2, ensure_ascii=False))
    else:
        summarize(orders)

    if not args.no_save:
        print(f"\nSaved {saved} order file(s) to {args.out_dir}/")


if __name__ == "__main__":
    main()
