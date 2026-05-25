# dwb_orders

A small, dependency-free Python CLI that pulls orders from the
[Digital Waybill API](https://github.com/digwaybill/Digital-Waybill-API) and
saves each order as its own JSON file.

It pages through `GET /{CID}/orders.json`, handles the API's Windows-1252
encoding, masks your API key in log output, and writes one
`order_<number>.json` file per order.

## Requirements

- Python 3.9+
- No third-party packages (standard library only)
- [uv](https://docs.astral.sh/uv/) is the recommended way to run it

## Setup

Clone the repo, then copy the example env file and fill in your credentials:

```bash
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
