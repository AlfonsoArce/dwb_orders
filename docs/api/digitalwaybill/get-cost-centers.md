# Get All Cost Centers (Cost center list)

Official Digital Waybill API endpoint ([official documentation](https://github.com/digwaybill/Digital-Waybill-API)). Returns the paginated flat list of cost centers on the account. Cost centers are the billing entities that also appear nested under each customer in [get-customers.md](get-customers.md); this endpoint lists them standalone and can filter them by customer.

The API is a gateway (`api.dwaybill.com`) that proxies requests to the courier's **OrderPanel** — the dispatch application must be running and connected for requests to succeed (see gateway errors in [get-customers.md](get-customers.md#errors)).

> Field *values* shown below are lightly genericized (contact details) — the live response carries real customer data. Structure, field names, and IDs are verbatim.

## Request

```
GET https://api.dwaybill.com/{cid}/cost_centers.json?v=1&key={api_key}
```

The first path segment is the courier's **CID** (`DWB_CID` in `.env` — never hard-code it). Always use HTTPS — the key travels in the query string. Keep credentials in `.env` (`DWB_CID`, `DWB_KEY`), not in committed files.

### Authentication

Same two key types as the customer list (see [get-customers.md](get-customers.md#authentication)):

- **RemotePanel key** (the one in `.env`): returns all cost centers; optionally filter with `customer_number` or `customer_name`.
- **QuickEntry key**: requires `customer_number` + `password` credentials and is automatically limited to the authenticated customer's cost centers.

### Query parameters

| Parameter | Required | Description |
|---|---|---|
| `v` | yes | API version. `1` and `1.0` are both accepted; omitting it returns `400 Invalid or missing API version number.` |
| `key` | yes | API key. Missing → `401 Missing API key.`; wrong → `401 Invalid or unauthorized API key.` |
| `customer_number` | no | Limit results to the cost centers of the customer with this exact customer number (verified: `customer_number=976` returns just that customer's cost center). Same behavior as on `/customers.json`. |
| `customer_name` | no | Limit results to the cost centers of customers with this exact name, case-insensitive (verified). |
| `page_num` / `page_number` | no | 1-based page index. `page_num` is the official parameter, `page_number` a documented alias; both verified to work (`page_num` wins if both are sent). Defaults to `1`. The response always echoes it as `page_number`. |
| `page_size` | no | Records per page, 1–100 (same limits as `/customers.json`). Defaults to `50`. |

### Example

```bash
curl --request GET \
  --url "https://api.dwaybill.com/${DWB_CID}/cost_centers.json?v=1&key=${DWB_KEY}&page_size=100&page_num=2"
```

Scoped to one customer:

```bash
curl --request GET \
  --url "https://api.dwaybill.com/${DWB_CID}/cost_centers.json?v=1&key=${DWB_KEY}&customer_number=976"
```

## Response

`200 OK` with the standard `{status, error, body}` envelope (JSON `status` always equals the HTTP status; `error` is empty on success). A filter with no matches returns `200` with an empty array, not `404`.

```json
{
  "status": 200,
  "error": "",
  "body": {
    "count": 907,
    "page_number": 1,
    "page_size": 50,
    "cost_centers": [
      {
        "id": "1",
        "name": "DASI, LLC.",
        "contact": "A. CONTACT",
        "address": "10000 NW 25 STREET",
        "suite": "",
        "city": "MIAMI",
        "state": "FL",
        "postal_code": "33172",
        "country": "United States",
        "email": "contact@example.com",
        "phone": "305-555-0133",
        "fax": "305-555-0100"
      }
    ]
  }
}
```

### Record fields (`CostCenter` type)

Matches the official `CostCenter` type exactly — the same object shape as the `cost_centers` entries nested in a `Customer`. All fields are strings.

| Field | Type | Description |
|---|---|---|
| `id` | string | Cost center ID (numeric string). Matches the `cost_centers[].id` values nested in the customer list. |
| `name` | string | Cost center display name. |
| `contact` | string | Contact person name; often empty or a placeholder (`.`). |
| `address`, `suite`, `city`, `state`, `postal_code`, `country` | string | Structured address. |
| `email` | string | Free-form and unvalidated — observed values include `.` and street-address text alongside real addresses. |
| `phone`, `fax` | string | Free-form; formats vary, including two numbers in one field with an annotation, like `305-555-0157//305-555-0108 CEL`. Often empty. |

Records carry **no parent-customer field**. To find a cost center's customer, either filter this endpoint with `customer_number`/`customer_name`, or match the nested `cost_centers[].id` in [get-customers.md](get-customers.md).

> Official caveat: the gateway may return extra fields not in the published docs (deprecated or not yet provisioned); relying on them is unsupported.

### Errors

Same two layers as the customer list — see [get-customers.md](get-customers.md#errors) for the full tables:

- **API errors** in the JSON envelope: `400` (missing `v`), `401` (missing/invalid key or QuickEntry credentials), `403` (QuickEntry client attempting to access another customer's cost centers).
- **Gateway errors** with an empty HTTP body: `400`, `404` (unknown CID), `502`, `503` (OrderPanel offline — retry with exponential backoff).

## Pagination

Standard page/size scheme: offset is `(page_num - 1) * page_size`, `page_size` capped at 100. `body.count` is the total (907 on this account, 2026-07-14 — slightly more than the 903 customers, so a few customers have multiple cost centers or orphaned ones exist). A page past the end returns `200` with an empty `cost_centers` array (not an error), which also works as a loop terminator.

## Notes

- Ordering is by `id` ascending (oldest first) — the **opposite** of the customer list, which returns newest first. Not specified in the official docs.
- `HEAD` requests are not supported (the gateway returns `502`); use `GET`.
- The order-placement API (`POST /orders.json`) takes a `cost_center` value that must belong to the order's `customer_number` — this list is where valid values come from.

## Related

- [get-customers.md](get-customers.md) — customer list; each customer embeds its cost centers with the same field structure.
- [Official API documentation](https://github.com/digwaybill/Digital-Waybill-API) — also covers `/orders.json` (GET/POST), `/packages.json`, and `/service_types.json`, not yet documented here.
