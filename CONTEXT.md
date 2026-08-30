# Orders

Delivery orders placed with HawkExpress, a Miami courier operation, and
retrieved from the Digital Waybill dispatch system. This context covers the
orders themselves, the stops that make up each one, and the customers they
are billed to.

## Orders

**Order**:
A single courier job: one customer's request to move something between two or
more locations on a given day.
_Avoid_: Job, delivery, shipment, waybill

**Order Number**:
The business identifier for an Order, used on invoices, in reports, and in
conversation. Distinct from the dispatch system's own internal record id,
which is an implementation artifact and never used to refer to an Order.
_Avoid_: Order id, id

**Order Status**:
Where an Order sits in its lifecycle: Confirmed, Dispatched, PickedUp,
Completed, or Cancelled.
_Avoid_: State, status (unqualified)

**Terminal Status**:
An Order Status that will never change again — Completed or Cancelled. Any
other status is **In Flight** and may still be updated by dispatch.
_Avoid_: Final, closed, done

**Order Flags**:
Read-state markers on an Order — pending, flagged, read — set by dispatch
staff working their queue. These describe whether a human has looked at the
Order, not where it is in its lifecycle, and are unrelated to Order Status.
_Avoid_: Flag status, status

**Revision**:
The dispatch system's marker for when an Order last changed — `version` in the
API. A stored Order is replaced only by one carrying a newer Revision, which
is what makes re-fetching and re-importing safe.
_Avoid_: Version, timestamp, updated_at

**Order Type**:
What kind of job the Order is: Delivery, Pickup, or Third-Party. Describes the
Order as a whole and has no relationship to the roles of individual stops.
_Avoid_: Service, category

**Origin**:
How the Order reached HawkExpress — by phone, through the web, and so on.
_Avoid_: Source (which means something else here), channel

**Source**:
The dispatch system an Order was ingested from. Digital Waybill is the only
Source today; the term exists because Orders from a second system would carry
their own, independently numbered Order Numbers.
_Avoid_: System, provider, origin

## Stops

**Route Stop**:
One place an Order calls at, in the order the driver visits them. Most Orders
have two, but three and five occur.
_Avoid_: Leg, waypoint, address

**First Stop / Final Stop**:
The first and last Route Stops of an Order — where the job starts and where it
ends. Named positionally because an Order may have more than two stops, which
makes "the pickup" and "the delivery" ambiguous.
_Avoid_: Pickup stop, delivery stop, origin stop, destination

**Stop Status**:
The state of an individual Route Stop, recorded by the dispatch system as a
numeric code. Unrelated to Order Status.
_Avoid_: Route status, status

**Service Type**:
The class of vehicle or handling a Route Stop requires, such as a 16 ft box
truck.
_Avoid_: Vehicle type, order type

## Pricing

**Final Price**:
The amount an Order is billed at — the sum of its Charges. Distinct from the
price first quoted when the Order was placed.
_Avoid_: Price (unqualified), total, cost

**Charge**:
One line of an Order's Final Price: a description with a quantity, a rate, and
a Pricing Code. Waiting time, extra stops, and fuel surcharges are Charges
alongside the base price.
_Avoid_: Line item, fee, surcharge (one kind of Charge)

**Price Breakdown**:
The complete list of an Order's Charges. The dispatch system reports it only
in History exports; its API gives the Final Price as a single number.
_Avoid_: Price details, itemisation

**Pricing Code**:
How a Charge's rate was set: auto-priced from the pricelist (AP), entered
manually by a dispatcher (M), or added automatically as a surcharge (AS).
_Avoid_: Charge type, rate type

## Customers and places

**Customer**:
The account an Order is billed to — typically an airline or MRO, such as
Lufthansa Technik. Identified by its Customer Number; the dispatch system
records its name in the cost centre field, which is free text and does contain
typos.
_Avoid_: Client, account, cost center, company

**Customer Number**:
The stable identifier for a Customer. One Customer Number corresponds to one
Customer.
_Avoid_: Account number, client id

**Company**:
The business at a Route Stop. Often not the Customer: a Customer's freight is
routinely collected from or delivered to a third party's premises.
_Avoid_: Customer, client, account

**Location**:
A Company at a specific address, including its suite. Two Companies at the same
street address in different suites are different Locations — a single building
routinely houses many businesses, so a street address alone never identifies
one.
_Avoid_: Address, site, premises

**Driver**:
The courier assigned to carry out an Order, identified by initials or a driver
number.
_Avoid_: Courier, operator
