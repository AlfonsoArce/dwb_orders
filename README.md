# dwb_orders

A small Python CLI that pulls orders from the
[Digital Waybill API](https://github.com/digwaybill/Digital-Waybill-API) and
saves each order as its own JSON file.

It pages through `GET /{CID}/orders.json`, handles the API's Windows-1252
encoding, shows a progress bar, redacts your API key from all log output, and
writes one `order_<number>.json` file per order. Orders in a terminal state
(completed/cancelled) that are already saved are skipped on re-runs.

## Requirements

- Python 3.9+
- [`tqdm`](https://github.com/tqdm/tqdm) (the only third-party dependency)
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

# re-download terminal orders even if already saved
uv run get_orders.py --no-skip-terminal

# keep the console to the progress bar, capture full logs to a file
uv run get_orders.py --console-level ERROR --log-level DEBUG --log-file logs/fetch.log
```

Run `uv run get_orders.py --help` for the full list of flags.

## Output

By default each order is written to `./output/order_<number>.json`. The
`output/` directory is git-ignored because fetched orders contain real
customer data (names, addresses, phone numbers, pricing).

## Security notes

- Never commit `.env` or anything under `output/` — both are git-ignored.
- The API key is passed as a query-string parameter (per the API spec); the
  script masks it in any URL it prints.

## License

[MIT](LICENSE)
