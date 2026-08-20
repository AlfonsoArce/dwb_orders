-- Where a bulk import of the JSON archive got to.
--
-- Reading 169,000 files out of a synced folder takes long enough that an
-- interruption is a normal event, not an exception. Each batch records the
-- last file it handled, so a resumed run starts near there instead of at the
-- beginning. Resuming may re-read a file or two; the revision-guarded upsert
-- makes that a no-op.

create table import_progress (
    source_key      text        not null references sources (source_key),
    input_dir       text        not null,
    last_file       text,
    files_done      bigint      not null default 0,
    orders_loaded   bigint      not null default 0,
    stops_loaded    bigint      not null default 0,
    started_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),

    primary key (source_key, input_dir)
);
