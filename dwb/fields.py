"""The Digital Waybill Order and Route Stop field names, in one place.

Both spreadsheet exporters — the fetcher's workbook and the route-stops
flattener — lay their columns out in a fixed order rather than deriving it
from whatever the API happened to return, so a workbook has the same shape
every run. That fixed order is these tuples, and they are defined here once
because two copies of a column list drift the moment the API gains a field.

Column *layout* still belongs to each exporter: they prefix, suffix and drop
different things. Only the underlying field names are shared.
"""

# Order-level fields, in the order they appear as columns.
ORDER_FIELDS = (
    "id",
    "order_number",
    "time",
    "status",
    "status_date",
    "status_detail",
    "origin",
    "order_type",
    "price",
    "final_price",
    "customer_number",
    "cost_center",
    "dispatch_driver",
    "ready_time",
    "deliver_by",
    "flagged",
    "read",
    "pending",
    "comm_override",
    "recurring_name",
    "optimized_route",
    "version",
)

# Route Stop fields, in the order they appear as columns.
STOP_FIELDS = (
    "route_stop_id",
    "company",
    "address",
    "suite",
    "city",
    "state",
    "postal_code",
    "country",
    "service_type",
    "package",
    "number_of_pieces",
    "weight",
    "vehicle",
    "driver_number",
    "paper_waybill",
    "special_instructions",
    "return_add",
    "dispatch_message",
    "notes",
    "signature_contact",
    "reference",
    "signature",
    "fuel_surcharge",
    "route_status",
    "route_status_detail",
    "route_status_date",
    "distance",
    "air_distance",
    "driver_pricelist",
    "receive_date",
    "dispatch_date",
    "pickup_date",
    "delivery_date",
    "cancel_date",
    "confirm_date",
)

# The signature_lines SVG runs to tens of KB per stop — useless in a
# spreadsheet and it would blow up the file size, so it never reaches a cell.
DROPPED_STOP_FIELDS = ("signature_lines",)
