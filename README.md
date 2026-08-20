# dwb_orders

A small Python CLI that pulls orders from the
[Digital Waybill API](https://github.com/digwaybill/Digital-Waybill-API) and
saves each order as its own JSON file.

It pages through `GET /{CID}/orders.json`, handles the API's Windows-1252
encoding, shows a progress bar, redacts your API key from all log output, and
writes one `order_<number>.json` file per order plus an Excel workbook
(`output/orders.xlsx`). An order is only written when it isn't already on disk,
so re-runs cost no writes.

## What's in the repo

- [`get_orders.py`](get_orders.py) — fetch orders from the Digital Waybill API
  (writes the per-order JSON files and, by default, an `orders.xlsx` workbook)
- [`process_route_stops.py`](process_route_stops.py) — flatten every `route_stop`
  from downloaded orders into a single `.xlsx` (one row per stop)
- [`route_stops_by_cost_center.py`](route_stops_by_cost_center.py) — group
  unique stop locations under each `cost_center` into a two-sheet `.xlsx`
  (summary + locations)
- [`extract_invoice_orders.py`](extract_invoice_orders.py) — read HawkExpress
  invoice PDFs under `input/invoices/<vendor>/*.pdf` and emit a three-sheet
  `.xlsx` (invoices / orders / adjustments)

## Requirements

- Python 3.9+
- Third-party packages (declared in [`pyproject.toml`](pyproject.toml)):
  [`tqdm`](https://github.com/tqdm/tqdm),
  [`xlsxwriter`](https://github.com/jmcnamara/XlsxWriter),
  [`pdfplumber`](https://github.com/jsvine/pdfplumber) (invoice PDF parsing)
- [uv](https://docs.astral.sh/uv/) is the recommended way to run it

## Setup

Clone the repo, install the dependency, then copy the example env file and fill
in your credentials:

```bash
uv sync                 # installs dependencies from pyproject.toml / uv.lock

cp .env.example .env
# edit .env and set DWB_CID and DWB_KEY
```

`.env` is git-ignored, so your real key is never committed.

| Variable              | Required | Description                                     |
| --------------------- | -------- | ----------------------------------------------- |
| `DWB_CID`             | yes      | Company/account id (the `{CID}` path segment)   |
| `DWB_KEY`             | yes      | API key                                         |
| `DWB_CUSTOMER_NUMBER` | no       | Customer number (QuickEntry / customer-scoped)  |
| `DWB_PASSWORD`        | no       | Password (QuickEntry / customer-scoped)         |

Credential precedence is **CLI flag > environment variable > `.env` file**.

## Usage

```bash
# with uv (no virtualenv needed)
uv run get_orders.py

# or plain Python
python3 get_orders.py
```

Common options:

```bash
# fetch more than the default single page
uv run get_orders.py --max-pages 20 --page-size 50

# print a summary only, don't write files
uv run get_orders.py --no-save

# dump the full JSON of every order to stdout
uv run get_orders.py --raw

# write order files somewhere else
uv run get_orders.py --out-dir ./data

# put the workbook elsewhere, or skip it entirely
uv run get_orders.py --excel reports/orders.xlsx
uv run get_orders.py --no-excel

# incremental fetch, but rebuild the workbook from the whole archive on disk
uv run get_orders.py --incremental --excel-from-dir

# re-save orders that are already on disk
uv run get_orders.py --overwrite            # all of them
uv run get_orders.py --refresh-stale        # only ones stored mid-flight

# keep the console to the progress bar, capture full logs to a file
uv run get_orders.py --console-level ERROR --log-level DEBUG --log-file logs/fetch.log
```

Run `uv run get_orders.py --help` for the full list of flags.

### When an order gets written

One scan of `--out-dir` builds the set of `order_number`s already on disk, and
that decides every write:

| Case | Written? |
| ---- | -------- |
| No file for that `order_number` | yes |
| File already exists | **no** — this is the default, and it's why a re-run costs zero writes |
| File exists, `--refresh-stale` and the *stored* copy isn't completed/cancelled | yes |
| File exists, `--overwrite` | yes |

The default means an order first captured while still in flight (`New`,
`Confirmed`, `PickedUp`, `Dispatched` — about 1% of the archive) keeps that
early snapshot and never picks up its completed state. `--refresh-stale` is the
targeted fix: it reads each already-saved copy's status and rewrites only the
ones that hadn't finished, leaving the immutable ones alone. `--overwrite`
(formerly `--no-skip-terminal`, still accepted) rewrites everything.

Avoiding needless writes matters most when `--out-dir` sits in a synced folder
such as iCloud Drive — every rewrite is another sync round-trip and another
chance of a `order_1234 2.json` conflict copy. Keeping the archive on a local
disk (`--out-dir ~/dwb_data/orders`) avoids that entirely.

### The Excel workbook

Every run also writes `output/orders.xlsx` (override with `--excel PATH`,
disable with `--no-excel`) covering the orders fetched in that run:

| Sheet         | Contents                                                                 |
| ------------- | ------------------------------------------------------------------------ |
| `orders`      | one row per order — the order-level fields plus `flag_status`, `stop_count`, and the first/last stop's company + city |
| `route_stops` | one row per stop, keyed by `order_number` with `stop_index` — drop it with `--no-excel-stops` |

The `signature_lines` SVG is never written to a cell (tens of KB per stop), and
text over Excel's 32,767-character cell limit is truncated with a marker.

Under `--incremental` a run only fetches new orders, so the workbook would cover
just those. Add `--excel-from-dir` to build it from every `order_*.json` in
`--out-dir` instead. A worksheet holds ~1.05M rows; past that the run warns and
`process_route_stops.py` is the way to get every stop.

### Post-processing the downloaded orders

After you've populated `output/` (or whichever `--out-dir` you used), the two
helper scripts turn the JSON files into Excel workbooks:

```bash
# one row per route_stop, prefixed with order-level context
uv run process_route_stops.py --input-dir output --output output/route_stops.xlsx

# distinct stop locations grouped by cost_center (two sheets: summary + locations)
uv run route_stops_by_cost_center.py --input-dir output --output output/route_stops_by_cost_center.xlsx
```

Use `--help` on either script for the full set of flags (sheet name, row limit,
log level/file, etc.).

### Extracting orders from HawkExpress invoice PDFs

Drop the invoice PDFs under `input/invoices/<vendor>/*.pdf`, then:

```bash
uv run extract_invoice_orders.py
# or with explicit paths:
uv run extract_invoice_orders.py --input-dir input/invoices --output output/invoice_orders.xlsx
```

The script parses each invoice's line-item table by column position (the table
layouts pdfplumber returns are inconsistent across vendors), pulls
order-number / From / To / service / reference / amount per line, attaches
per-order discount lines, and writes three sheets:

- **invoices** — one row per invoice header (vendor, invoice #, dates, totals)
- **orders** — one row per `#NNNNNN` order line, with `amount` + `discount_amount`
- **adjustments** — invoice-level lines without an order # (global discounts,
  waiting-time fees, after-hour fees, etc.)

Each invoice's `total` equals `sum(orders.amount) + sum(orders.discount_amount)
+ sum(adjustments.amount)`.

## Output

By default each order is written to `./output/order_<number>.json`, and the
post-processing scripts write their `.xlsx` files under `output/` too. The
`output/` directory is git-ignored because fetched orders contain real
customer data (names, addresses, phone numbers, pricing).

## Security notes

- Never commit `.env` or anything under `output/` — both are git-ignored.
- The API key is passed as a query-string parameter (per the API spec); the
  script masks it in any URL it prints.

## License

[MIT](LICENSE)
