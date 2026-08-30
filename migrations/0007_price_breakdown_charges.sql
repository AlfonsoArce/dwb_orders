-- Charges: the individual lines that make up an Order's Final Price.
--
-- The API reports Final Price as a single number; the itemisation — base
-- price, waiting time, extra stops — exists only in a History export's
-- PriceBreakdown column (parsed by dwb/price_breakdown.py). So these tables
-- are filled by an enrichment run over an export, not by ingest, and an Order
-- no export has covered simply has no rows here.
--
-- Same hybrid shape as everywhere: typed numeric columns for what reports sum
-- and filter on, plus the packed source string kept per Order, so a parse we
-- later distrust can be re-read without the 460 MB workbook it came from.

-- One row per enriched Order: the PriceBreakdown string as exported, and how
-- its parse related to the export's own FinalPrice at the time.
create table order_price_breakdowns (
    order_id            bigint      primary key,
    source_key          text        not null,

    breakdown           text        not null,   -- "PriceBreakdown", verbatim
    source_file         text        not null,   -- which export supplied it

    -- The export's own FinalPrice, and whether the parsed Charges sum to it
    -- (within the half cent the source system's rounding allows). The price
    -- is kept beside the flag so a false can be audited without the workbook.
    export_final_price  numeric(12, 2),
    matches_final_price boolean     not null,

    enriched_at         timestamptz not null default now(),

    constraint order_price_breakdowns_order_fk foreign key (order_id, source_key)
        references orders (order_id, source_key) on delete cascade
);


-- One row per Charge, in the order the breakdown lists them.
create table order_charges (
    order_charge_id     bigint generated always as identity primary key,

    order_id            bigint      not null
                        references order_price_breakdowns (order_id)
                        on delete cascade,
    charge_position     integer     not null check (charge_position >= 1),

    description         text        not null,

    -- Quantity and rate as the dispatcher entered them; null where a token
    -- did not parse as a number (dwb/price_breakdown.py's policy). The amount
    -- is derived in the database so it cannot disagree with its factors.
    quantity            numeric,
    rate                numeric,
    amount              numeric generated always as (quantity * rate) stored,

    -- Pricing Code: AP auto-priced base, M manual, AS auto surcharge.
    pricing_code        text,

    -- Reference tokens as parsed, usually an empty marker plus the dispatch
    -- system's record id. Uninterpreted, kept whole (ADR-0004's spirit).
    refs                jsonb       not null default '[]',

    constraint order_charges_order_position_key unique (order_id, charge_position)
);

-- Reports group by what a Charge is for — waiting time, extra stops, fuel.
create index order_charges_description_idx on order_charges (description);
