# Get All Customers (Customer list)

Official Digital Waybill API endpoint ([official documentation](https://github.com/digwaybill/Digital-Waybill-API)). Returns the paginated list of customers on the account, each with its nested cost centers.

The API is a gateway (`api.dwaybill.com`) that proxies requests to the courier's **OrderPanel** — the dispatch application must be running and connected for requests to succeed (see gateway errors below).

> Field *values* shown below are lightly genericized (contact details) — the live response carries real customer data. Structure, field names, and IDs are verbatim.

## Request

```
GET https://api.dwaybill.com/{cid}/customers.json?v=1&key={api_key}
```

The first path segment is the courier's **CID** (`DWB_CID` in `.env` — never hard-code it). HTTPS on 443 and plaintext HTTP on 80 are both accepted — always use HTTPS, since the key travels in the query string. Keep credentials in `.env` (`DWB_CID`, `DWB_KEY`), not in committed files.

### Authentication

Two kinds of API key exist, managed in the OrderPanel under Settings > Advanced (regenerating a key immediately invalidates the old one):

| Key type | Scope | Extra credentials |
|---|---|---|
| **RemotePanel** | Full access to all orders, customers, and couriers. The key in `.env` is this type. | none |
| **QuickEntry** | One customer's data only. | `customer_number` + `password` query parameters required |

With a QuickEntry key, this method returns only the authenticated customer; requesting another customer yields `403 Forbidden`.

### Query parameters

| Parameter | Required | Description |
|---|---|---|
| `v` | yes | API version. `1` and `1.0` are both accepted (verified); omitting it returns `400 Invalid or missing API version number.` |
| `key` | yes | API key. Missing → `401 Missing API key.`; wrong → `401 Invalid or unauthorized API key.` |
| `customer_number` | no | With a RemotePanel key: filter to the customer with this exact customer number (official docs say case-sensitive). With a QuickEntry key: part of the credentials. |
| `customer_name` | no | Filter to customers with this exact name, case-insensitive (verified: `proponent` matches `PROPONENT`). |
| `page_num` / `page_number` | no | 1-based page index. `page_num` is the official parameter, `page_number` a documented alias; both verified to work (`page_num` wins if both are sent). Defaults to `1`. |
| `page_size` | no | Records per page, 1–100. Defaults to `50`; values above 100 are clamped to 100 (verified with `page_size=200`). |

### Fetch a single customer

Append the numeric customer `id` (not `customer_number`) to the path. The response uses the same list envelope with `count: 1`:

```
GET https://api.dwaybill.com/{cid}/customers.json/1534?v=1&key={api_key}
```

### Example

```bash
curl --request GET \
  --url "https://api.dwaybill.com/${DWB_CID}/customers.json?v=1&key=${DWB_KEY}&page_num=1&page_size=50"
```

## Response

`200 OK` with the standard `{status, error, body}` envelope. Per the official docs, JSON `status` always equals the HTTP status code; `error` carries any error/warning text and `body` the payload. A filter with no matches returns `200` with an empty array, not `404`.

```json
{
  "status": 200,
  "error": "",
  "body": {
    "count": 903,
    "page_number": 1,
    "page_size": 50,
    "customers": [
      {
        "id": "1534",
        "customer_number": "976",
        "name": "PROPONENT",
        "contact": "",
        "address": "10601 State St",
        "suite": "1",
        "city": "TAMARAC",
        "state": "FL",
        "postal_code": "33321",
        "country": "United States",
        "email": "contact@example.com",
        "phone": "",
        "fax": "",
        "cost_centers": [
          {
            "id": "1546",
            "name": "PROPONENT",
            "contact": "",
            "address": "10601 State St",
            "suite": "1",
            "city": "TAMARAC",
            "state": "FL",
            "postal_code": "33321",
            "country": "United States",
            "email": "contact@example.com",
            "phone": "",
            "fax": ""
          }
        ]
      }
    ]
  }
}
```

### Record fields (`Customer` type)

Matches the official `Customer` type exactly — all fields are strings except `cost_centers`.

| Field | Type | Description |
|---|---|---|
| `id` | string | Internal customer ID (numeric string). Usable in the `/customers.json/{id}` path. |
| `customer_number` | string | Human-facing customer number, distinct from `id`. Usable as the `customer_number` filter. |
| `name` | string | Customer display name. Usable as the `customer_name` filter (case-insensitive). |
| `contact` | string | Contact person name; often empty. |
| `address`, `suite`, `city`, `state`, `postal_code`, `country` | string | Structured address. |
| `email` | string | May contain **multiple comma-separated addresses** in one string. |
| `phone`, `fax` | string | Free-form; formats vary (`4808674404`, `954-6218658`, `888-613-4143 ext. 701`). Often empty. |
| `cost_centers` | array | `CostCenter` objects (same shape as [get-cost-centers.md](get-cost-centers.md)) under the customer. Each has its **own `id`** plus the same contact/address fields (no `customer_number`). In observed data most customers have exactly one cost center duplicating the customer's details. |

> Official caveat: the gateway may return extra fields not in the published docs (deprecated or not yet provisioned); relying on them is unsupported.

### Errors

Two layers produce errors:

**API errors** — the standard envelope (HTTP status = JSON `status`, message in `error`, empty `body`):

| HTTP / `status` | `error` | Cause |
|---|---|---|
| `400` | `Invalid or missing API version number.` | `v` missing. |
| `401` | `Missing API key.` / `Invalid or unauthorized API key.` | `key` missing, wrong, or QuickEntry credentials invalid. |
| `403` | — | QuickEntry client attempted to access another customer (official docs). |

**Gateway errors** — from `api.dwaybill.com` itself, with an **empty HTTP body** (no JSON envelope), per the official docs:

| HTTP status | Meaning |
|---|---|
| `400` | Malformed request URI. |
| `404` | Unknown CID (verified: bogus CID returns a bodyless 404). |
| `502` | Gateway servers unreachable — contact Digital Waybill support. |
| `503` | CID valid but the courier's OrderPanel is offline. Retry with **exponential backoff**. |

Clients should therefore handle both bodyless HTTP errors and enveloped JSON errors.

## Pagination

Standard page/size scheme: offset is `(page_num - 1) * page_size`, `page_size` capped at 100. `body.count` is the total number of customers (903 on this account, 2026-07-14), so fetch `ceil(count / page_size)` pages. A `page_num` past the end returns `200` with an empty `customers` array (not an error), which also works as a loop terminator.

## Notes

- Ordering appears to be by `customer_number` descending (newest first). Not specified in the official docs.
- `HEAD` requests are not supported (the gateway returns `502`); use `GET`.
- No rate-limit headers were observed.
- The official docs provide a public sandbox account for testing: CID `2000105850`, RemotePanel key `d9e8df23d7149ed1c70a9b98539ec776539ec776`, QuickEntry key `f1d621905cece65bcbbb5018adacdd39adacdd39` (customer `DYN833` / `pass`).

## Related

- [get-cost-centers.md](get-cost-centers.md) — flat list of all cost centers on the account; supports the same `customer_number`/`customer_name` filters to scope to one customer.
- [Official API documentation](https://github.com/digwaybill/Digital-Waybill-API) — also covers `/orders.json` (GET/POST), `/packages.json`, and `/service_types.json`, not yet documented here.
