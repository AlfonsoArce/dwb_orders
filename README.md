# dwb_orders

A small Python CLI that pulls orders from the
[Digital Waybill API](https://github.com/digwaybill/Digital-Waybill-API) and
saves each order as its own JSON file.

It pages through `GET /{CID}/orders.json`, handles the API's Windows-1252
encoding, shows a progress bar, and redacts your API key from all log output.

**Orders live in Postgres.** The database is the source of truth: the fetcher
writes every order it retrieves straight into it, and questions get answered in
SQL rather than by opening 169,000 files. The per-order `order_<number>.json`
files are still written, because the spreadsheet exporters still read them —
`--no-json` turns that off once they are migrated.

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
- [`migrate.py`](migrate.py) — apply pending database migrations from
  [`migrations/`](migrations)
- [`import_orders.py`](import_orders.py) — one-time (resumable) load of the
  existing JSON archive into the database
- [`verify_import.py`](verify_import.py) — prove the database is a faithful
  copy of the archive, and retire the archive once it is
- [`backup.sh`](backup.sh) — dump and restore the database, outside the synced
  folder
- [`dwb/`](dwb) — the shared modules the scripts import (field definitions,
  coercion rules, ingest, migrations runner)

## Requirements

- Python 3.9+
- Third-party packages (declared in [`pyproject.toml`](pyproject.toml)):
  [`tqdm`](https://github.com/tqdm/tqdm),
  [`xlsxwriter`](https://github.com/jmcnamara/XlsxWriter),
  [`psycopg`](https://www.psycopg.org/psycopg3/) (Postgres),
  [`pdfplumber`](https://github.com/jsvine/pdfplumber) (invoice PDF parsing)
- [uv](https://docs.astral.sh/uv/) is the recommended way to run it
- Docker, for the database and its browser client

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

## The database

One command brings up Postgres and [Adminer](https://www.adminer.org/), both on
loopback only. Postgres publishes port **5434** (5432 and 5433 already belong to
the TMS and billing databases), and its storage is a named Docker volume — never
a path inside iCloud Drive, which corrupts a Postgres data directory.

```bash
docker compose up -d          # database on 127.0.0.1:5434, Adminer on :8081
uv run python migrate.py      # apply any pending migrations
uv run python migrate.py --status
```

| Variable            | Description                                                     |
| ------------------- | --------------------------------------------------------------- |
| `POSTGRES_PASSWORD` | The database password. Written once, in `.env`.                 |
| `DWB_DSN`           | Connection string, **without** the password (see below)          |
| `DWB_PG_PORT`       | Host port for Postgres (default 5434)                            |
| `DWB_ADMINER_PORT`  | Host port for Adminer (default 8081)                             |

The connection string deliberately carries no password: `docker compose` and the
scripts both read `POSTGRES_PASSWORD`, so the same secret is never maintained in
two formats. Precedence for `--dsn` is the same as for the credentials.

Schema changes are numbered `.sql` files in [`migrations/`](migrations), applied
once each and recorded in `schema_migrations`; re-running the runner with
nothing pending does nothing. Never edit a migration that has been applied — the
runner refuses it.

### Loading the existing archive

```bash
uv run python import_orders.py --input-dir output/orders
```

Batched, and it records where it got to, so an interrupted run resumes near
where it stopped rather than starting over. Unreadable, empty and vanished files
are counted and reported separately — never silently skipped. Sync conflict
copies (`order_401791 2.json`) are matched, and the newest revision wins.

### Proving it is faithful, and backing it up

```bash
./backup.sh dump                                   # ~/dwb_orders_backups/*.dump
./backup.sh restore ~/dwb_orders_backups/FILE.dump # into dwb_orders_restored
uv run python verify_import.py --sample 500
```

`verify_import.py` reads every file in the archive, compares Order and Route
Stop counts against the database, and reconstructs a random sample of Orders
from the stored raw payloads to diff against their files. It is read-only and
repeatable, and it exits non-zero on any mismatch.

Once it passes and a dump exists, the archive can be retired — moved out of the
synced folder, never deleted:

```bash
uv run python verify_import.py --retire-to ~/dwb_orders_archive        # plan
uv run python verify_import.py --retire-to ~/dwb_orders_archive --yes  # do it
```

### Tests

```bash
uv run pytest
```

The suite creates its own disposable databases on the same server, applies the
migrations to them, drives the command-line entry points, and asserts what
ended up in SQL. The API is stubbed by a real HTTP server on loopback, so the
suite runs offline, consumes no rate limit, and leaves no database behind.

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

# store Orders in the database but stop writing the per-order JSON files
uv run get_orders.py --no-json

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

# incremental, re-reading the 10 most recent pages so In Flight Orders
# pick up their final status (the default overlap is 5 pages)
uv run get_orders.py --incremental --overlap-pages 10

# then chase the stragglers individually — opt-in, bounded, previewable
uv run get_orders.py --refresh-in-flight --sweep-limit 50
uv run get_orders.py --sweep-preview

# keep the console to the progress bar, capture full logs to a file
uv run get_orders.py --console-level ERROR --log-level DEBUG --log-file logs/fetch.log
```

Run `uv run get_orders.py --help` for the full list of flags.

### When an order gets written

This governs the **JSON files** only. The database has its own rule: an Order is
rewritten there when the dispatch system's revision marker (`version`) is newer
than the stored one, and otherwise not at all — which is what makes re-runs,
resumed imports and overlapping pages free.

Whether a file already exists for that `order_number` decides every file write:

| Case | Written? |
| ---- | -------- |
| No file for that `order_number` | yes |
| File already exists | **no** — this is the default, and it's why a re-run costs zero writes |
| File exists, `--refresh-stale` and the *stored* copy isn't completed/cancelled | yes |
| File exists, `--overwrite` | yes |

The default means an order first captured while still in flight (`New`,
`Confirmed`, `PickedUp`, `Dispatched` — 68 of the 168,986 Orders in the archive)
keeps that early snapshot in its *file* and never picks up its completed state.
`--refresh-stale` is the targeted fix for the files: it reads each already-saved
copy's status and rewrites only the ones that hadn't finished, leaving the
immutable ones alone. `--overwrite` (formerly `--no-skip-terminal`, still
accepted) rewrites everything. In the database, `--overlap-pages` and
`--refresh-in-flight` do this job instead, and do it properly.

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
