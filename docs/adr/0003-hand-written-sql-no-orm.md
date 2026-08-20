# Database access is hand-written SQL, not an ORM

We use psycopg with SQL written by hand, and deliberately no ORM. This
codebase is otherwise standard-library only, and the two operations that
matter — a bulk load of the existing order archive, and a version-guarded
upsert — are both plain SQL that an ORM would obscure rather than simplify.

## Consequences

This is a deliberate choice, not an omission. Introducing SQLAlchemy or a
similar layer later should be treated as reversing a decision, not as tidying
up.
