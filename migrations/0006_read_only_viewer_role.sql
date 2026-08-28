-- A role that can read the Order store and write to none of it.
--
-- The Orders viewer connects as this role. Read-only is enforced here, in
-- Postgres, rather than by the application only ever issuing selects: a code
-- convention holds until somebody writes one careless statement, whereas a
-- missing privilege holds regardless of what is written above it.
--
-- Two things about this migration are unusual, and both are deliberate.
--
-- Creating the role tolerates it already existing. A role is a cluster-level
-- object while migrations are applied per database, and the test suite applies
-- every migration to a freshly created database in the same cluster on every
-- run — so a plain `create role` would succeed exactly once and fail every run
-- afterwards. The grants below are database-scoped and so are unguarded.
--
-- Note this is written as try-then-handle rather than look-then-leap. The
-- runner's advisory lock does not help here: advisory lock tags include the
-- database, so `pytest` migrating its template database and `migrate.py`
-- migrating the store hold different locks and can both pass an `if not
-- exists` check before either creates the role. Catching duplicate_object has
-- no such window.
--
-- No password is set: this file is committed, and a password in version
-- control is a password that cannot be rotated by rotating it. The
-- password is set once by hand, alongside filling in .env — see the entry for
-- DWB_VIEWER_PASSWORD in .env.example for the command:
--
--   alter role dwb_viewer password '...'
--
-- Until that is done the role exists and cannot log in, which under
-- scram-sha-256 fails in a way that reads like a misconfiguration rather than
-- like a missing step.

do $$
begin
    create role dwb_viewer login;
exception
    when duplicate_object then null;   -- another database's migration got there first
end
$$;

-- Which database this is depends on where the migration is applied — the real
-- store, or one of the suite's disposable clones — so it is asked for rather
-- than named.
--
-- This narrows nothing today: PUBLIC holds connect on every database by
-- default, so the viewer can already reach this one and every other one in the
-- cluster. It is here so that the viewer keeps working in a cluster where that
-- default has been revoked, not as a claim that its access is scoped.
do $$
begin
    execute format('grant connect on database %I to dwb_viewer', current_database());
end
$$;

grant usage on schema public to dwb_viewer;

-- Every table the store has today. Select only: no insert, update, delete,
-- truncate or references.
grant select on all tables in schema public to dwb_viewer;

-- And every table it gains later. Without this, migration 0007 adds a table
-- the viewer cannot read, and nothing says so until someone opens the page —
-- the grant is easy to forget precisely because forgetting it is silent.
--
-- Default privileges attach to the role that creates the objects, which is the
-- role migrations run as. That is the same role now and later, so this holds.
alter default privileges in schema public grant select on tables to dwb_viewer;
