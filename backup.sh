#!/usr/bin/env bash
#
# backup.sh — dump and restore the Order store.
#
# The dump goes outside the synced folder, on purpose. A backup that lives in
# iCloud next to the thing it is backing up is not a backup, and this database
# is about to become the only copy of 169,000 Orders.
#
#   ./backup.sh dump                 write a new dump to the backup directory
#   ./backup.sh list                 show the dumps taken so far
#   ./backup.sh restore FILE [DB]    restore a dump into a database (default
#                                    dwb_orders_restored), creating it first
#
# Override the destination with DWB_BACKUP_DIR. Nothing is scheduled: taking a
# backup is a decision, made before anything irreversible.

set -euo pipefail

CONTAINER="${DWB_DB_CONTAINER:-dwb-orders-db}"
DB_USER="${DWB_DB_USER:-dwb}"
DB_NAME="${DWB_DB_NAME:-dwb_orders}"
BACKUP_DIR="${DWB_BACKUP_DIR:-$HOME/dwb_orders_backups}"

die() { printf '%s\n' "$*" >&2; exit 1; }

require_container() {
    docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null \
        | grep -q true || die "Container $CONTAINER is not running. Start it with: docker compose up -d"
}

check_destination() {
    case "$BACKUP_DIR" in
        *"Mobile Documents"*|*"Dropbox"*|*"Google Drive"*)
            die "Refusing to write backups into a synced folder: $BACKUP_DIR" ;;
    esac
    mkdir -p "$BACKUP_DIR"
}

cmd_dump() {
    require_container
    check_destination
    local stamp file
    stamp="$(date +%Y%m%dT%H%M%S)"
    file="$BACKUP_DIR/${DB_NAME}_${stamp}.dump"
    # Custom format: compressed, and restorable selectively with pg_restore.
    docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$file"
    printf 'Wrote %s (%s)\n' "$file" "$(du -h "$file" | cut -f1)"
}

cmd_list() {
    check_destination
    ls -lh "$BACKUP_DIR"/*.dump 2>/dev/null || echo "No dumps in $BACKUP_DIR yet."
}

cmd_restore() {
    require_container
    local file="${1:-}" target="${2:-${DB_NAME}_restored}"
    [ -n "$file" ] || die "Usage: ./backup.sh restore FILE [DATABASE]"
    [ -f "$file" ] || die "No such dump: $file"
    [ "$target" != "$DB_NAME" ] || \
        die "Refusing to restore over $DB_NAME. Name a different database."

    docker exec "$CONTAINER" dropdb -U "$DB_USER" --if-exists --force "$target"
    docker exec "$CONTAINER" createdb -U "$DB_USER" "$target"
    # --no-privileges, not just --no-owner: since migration 0006 the dump
    # carries GRANTs to dwb_viewer, and roles are cluster-level so no database
    # dump creates them. Restoring onto a fresh cluster — the disaster this
    # script exists for — would fail every one of those GRANTs, and pg_restore
    # exiting non-zero under `set -e` would abort before the row counts below
    # ever ran. Privileges come back by applying migrations, not from the dump.
    docker exec -i "$CONTAINER" pg_restore -U "$DB_USER" -d "$target" \
        --no-owner --no-privileges < "$file"
    docker exec "$CONTAINER" psql -U "$DB_USER" -d "$target" -c \
        "select 'orders' as table, count(*) from orders
         union all select 'route_stops', count(*) from route_stops"
    printf 'Restored %s into database %s\n' "$file" "$target"
}

case "${1:-}" in
    dump)    shift; cmd_dump "$@" ;;
    list)    shift; cmd_list "$@" ;;
    restore) shift; cmd_restore "$@" ;;
    *)       die "Usage: ./backup.sh {dump|list|restore FILE [DATABASE]}" ;;
esac
