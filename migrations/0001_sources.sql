-- The dispatch systems Orders can be ingested from.
--
-- Digital Waybill is the only one today. The table exists so that a second
-- system's Orders — independently numbered, and certain to collide — can be
-- stored alongside without renumbering anything. See ADR-0002.

create table sources (
    source_key  text primary key,
    name        text not null,
    added_at    timestamptz not null default now()
);

insert into sources (source_key, name)
values ('digital_waybill', 'Digital Waybill');
