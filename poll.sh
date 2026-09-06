#!/bin/sh
# The Orders poller's loop: fetch what is new, fill in what was missed, repeat.
#
# Run as PID 1 of the `poller` container (see Dockerfile). Everything it needs
# arrives as an environment variable, and everything it has to say goes to
# stdout, where `docker compose logs -f poller` will find it.
#
# Why --max-pages matters more than it looks
# ------------------------------------------
# get_orders.py defaults --max-pages to 1, which is right for a manual probe
# and quietly destructive on a schedule. An incremental run resumes from the
# highest Order Number stored, so a run that stops after one page keeps the
# newest fifty Orders, moves the watermark past everything it did not fetch,
# and never comes back for them. Two such runs left ~680 Orders missing from
# the store. --max-pages here is a *ceiling*, not a target: --incremental
# stops as soon as it reaches an Order already stored, so a quiet run costs
# six requests and the ceiling only matters after an outage.
#
# Why the gap fill exists anyway
# ------------------------------
# Because a ceiling can still be reached — a long outage, a bad afternoon on
# the API — and paging can never recover from that on its own: the holes it
# leaves sit *below* the watermark it resumes from. --fill-gaps names the
# absent Order Numbers and asks for them one at a time. It runs on its own,
# slower cadence, over a window of recent Orders rather than the whole store.
#
# Settings (all optional, all set in docker-compose.yml):
#   DWB_POLL_INTERVAL       seconds between runs           (default 300)
#   DWB_POLL_MAX_PAGES      catch-up ceiling, in pages     (default 200)
#   DWB_POLL_OVERLAP_PAGES  pages re-read for status moves (default 5)
#   DWB_POLL_PAGE_DELAY     seconds between requests       (default 0.5)
#   DWB_POLL_SWEEP          non-empty to re-request In Flight stragglers
#   DWB_POLL_FILL_EVERY     fill gaps every Nth run, 0 to never (default 12)
#   DWB_POLL_FILL_DAYS      how far back the fill looks     (default 90)
#   DWB_POLL_FILL_LIMIT     most Orders one fill requests   (default 500)
set -u

INTERVAL="${DWB_POLL_INTERVAL:-300}"
MAX_PAGES="${DWB_POLL_MAX_PAGES:-200}"
OVERLAP_PAGES="${DWB_POLL_OVERLAP_PAGES:-5}"
PAGE_DELAY="${DWB_POLL_PAGE_DELAY:-0.5}"
SWEEP="${DWB_POLL_SWEEP:-}"
FILL_EVERY="${DWB_POLL_FILL_EVERY:-12}"
FILL_DAYS="${DWB_POLL_FILL_DAYS:-90}"
FILL_LIMIT="${DWB_POLL_FILL_LIMIT:-500}"

# The fetch and the sleep both run in the background so that `wait` is what
# this shell is doing when a signal arrives. A foreground child would leave
# SIGTERM unhandled until it exited on its own — up to a full fetch — and
# `docker compose stop` would spend ten seconds waiting to give up and kill.
child=""
stopping=0

on_term() {
    stopping=1
    [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
    return 0
}
trap on_term TERM INT

say() {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') poll: $*"
}

say "starting: every ${INTERVAL}s, up to ${MAX_PAGES} page(s), overlap ${OVERLAP_PAGES}"
if [ "$FILL_EVERY" -gt 0 ]; then
    say "gap fill: every ${FILL_EVERY} run(s), over the last ${FILL_DAYS} day(s)"
else
    say "gap fill: disabled"
fi

run=0
while [ "$stopping" -eq 0 ]; do
    run=$((run + 1))

    set -- --incremental \
        --max-pages "$MAX_PAGES" \
        --overlap-pages "$OVERLAP_PAGES" \
        --page-delay "$PAGE_DELAY" \
        --no-json --no-excel
    [ -n "$SWEEP" ] && set -- "$@" --refresh-in-flight
    # The first run fills too: if the poller is starting after a stretch of
    # downtime, that is exactly when a hole is most likely to be waiting.
    if [ "$FILL_EVERY" -gt 0 ] && [ $(((run - 1) % FILL_EVERY)) -eq 0 ]; then
        set -- "$@" --fill-gaps --fill-days "$FILL_DAYS" --fill-limit "$FILL_LIMIT"
    fi

    python get_orders.py "$@" &
    child=$!
    wait "$child"
    status=$?
    child=""

    [ "$stopping" -eq 1 ] && break

    # Exit 2 is "the Order store cannot be reached" and 3 is "this run left a
    # hole it did not fill" — both worth saying out loud, neither worth
    # exiting for. The database is a sibling container and may be restarting;
    # a hole is what the next fill run is for.
    if [ "$status" -eq 2 ]; then
        say "the Order store is unreachable; retrying in ${INTERVAL}s"
    elif [ "$status" -eq 3 ]; then
        say "this run left a gap — see the error above; the next fill will collect it"
    elif [ "$status" -ne 0 ]; then
        say "fetch exited ${status}; retrying in ${INTERVAL}s"
    fi

    sleep "$INTERVAL" &
    child=$!
    wait "$child"
    child=""
done

say "stopped"
