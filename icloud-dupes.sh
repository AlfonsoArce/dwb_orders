#!/bin/zsh
# icloud-dupes.sh — find iCloud conflict copies ("name 2.ext") and tell you
# which ones are byte-identical to the original (safe to remove) and which
# ones actually differ (open them before deciding).
#
# Usage:
#   ./icloud-dupes.sh                     # scan iCloud Drive, report only
#   ./icloud-dupes.sh ~/Documents         # scan a specific folder
#   ./icloud-dupes.sh --quarantine        # ALSO move identical dupes aside
#   ./icloud-dupes.sh --quarantine ~/Docs
#   ./icloud-dupes.sh --no-progress       # plain output, no progress bar
#
# Nothing is ever deleted. --quarantine moves identical duplicates into
# ~/iCloud-Duplicates-<date>/ preserving their folder structure, so you can
# look them over and drag the folder to the Trash yourself.

emulate -L zsh
setopt extended_glob null_glob

QUARANTINE=0
PROGRESS=1
ROOT=""

for arg in "$@"; do
  case "$arg" in
    --quarantine|-q)  QUARANTINE=1 ;;
    --no-progress)    PROGRESS=0 ;;
    --help|-h)        sed -n '2,20p' "$0"; exit 0 ;;
    *)                ROOT="$arg" ;;
  esac
done

[[ -z "$ROOT" ]] && ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
ROOT="${~ROOT}"

if [[ ! -d "$ROOT" ]]; then
  print -u2 "Not a folder: $ROOT"
  exit 1
fi

# No bar when output is piped to a file or another command.
[[ -t 1 ]] || PROGRESS=0

QDIR="$HOME/iCloud-Duplicates-$(date +%Y-%m-%d)"

# ---------------------------------------------------------------- progress --
typeset -i BAR_WIDTH=32

draw_bar() {  # draw_bar <done> <total> <label>
  (( PROGRESS )) || return
  local -i done=$1 total=$2
  local label="$3"
  local -i pct=0 filled=0
  (( total > 0 )) && (( pct = done * 100 / total ))
  (( filled = BAR_WIDTH * pct / 100 ))

  local bar=""
  repeat $filled              { bar+="█" }
  repeat $(( BAR_WIDTH - filled )) { bar+="·" }

  # Trim the label so the whole line fits the terminal.
  local -i cols=${COLUMNS:-80}
  local -i room=$(( cols - BAR_WIDTH - 22 ))
  (( room < 8 )) && room=8
  (( ${#label} > room )) && label="…${label[-room+1,-1]}"

  printf '\r\e[2K  [%s] %3d%%  %d/%d  %s' "$bar" "$pct" "$done" "$total" "$label"
}

clear_bar() {
  (( PROGRESS )) && printf '\r\e[2K'
}

# Make sure a Ctrl-C doesn't leave the cursor parked on the bar.
TRAPINT() { clear_bar; print "\nInterrupted."; return 130 }

# ------------------------------------------------------------------ scan 1 --
print "Scanning: $ROOT"
(( PROGRESS )) && printf '  Looking for conflict copies…'

typeset -a candidates
while IFS= read -r -d '' f; do
  [[ "$f" == "$QDIR"* ]] && continue
  candidates+=("$f")
done < <(find "$ROOT" -type f -name "* [0-9].*" -print0 \
         -o -type f -name "* [0-9][0-9].*" -print0 \
         -o -type f -name "* [0-9]" -print0)

clear_bar
typeset -i total=${#candidates}
print "  Found $total candidate file(s) to check."
print ""

# ------------------------------------------------------------------ scan 2 --
typeset -i n_ident=0 n_diff=0 n_orphan=0 n_skip=0 n_moved=0 i=0
typeset -a identical differing orphans skipped

for dup in $candidates; do
  (( i++ ))
  draw_bar $i $total "${dup:t}"

  dir="${dup:h}"
  base="${dup:t}"

  if [[ "$base" == *.* ]]; then
    stem="${base%.*}"
    ext=".${base##*.}"
  else
    stem="$base"
    ext=""
  fi

  # stem must end with a space + digits, e.g. "report 2"
  [[ "$stem" == *" "<-> ]] || { orphans+=("$dup"); (( n_orphan++ )); continue }
  orig_stem="${stem% <->}"
  [[ -z "$orig_stem" ]] && { orphans+=("$dup"); (( n_orphan++ )); continue }

  orig="$dir/$orig_stem$ext"

  if [[ ! -e "$orig" ]]; then
    orphans+=("$dup"); (( n_orphan++ )); continue
  fi

  # Skip files that aren't fully downloaded (iCloud placeholders).
  if [[ -e "$dir/.$orig_stem$ext.icloud" || -e "$dir/.$base.icloud" ]]; then
    skipped+=("$dup"); (( n_skip++ )); continue
  fi

  if cmp -s "$dup" "$orig"; then
    identical+=("$dup"); (( n_ident++ ))
  else
    differing+=("$dup"); (( n_diff++ ))
  fi
done

clear_bar

# ----------------------------------------------------------------- report ---
print "=============================================================="
print "IDENTICAL to the original — safe to remove ($n_ident)"
print "=============================================================="
for f in $identical; do print "  ${f#$ROOT/}"; done
(( n_ident == 0 )) && print "  (none)"

print ""
print "=============================================================="
print "DIFFERENT from the original — review before deleting ($n_diff)"
print "=============================================================="
for f in $differing; do
  print "  ${f#$ROOT/}"
  print "      dup:  $(stat -f '%z bytes, modified %Sm' -t '%Y-%m-%d %H:%M' "$f")"
done
(( n_diff == 0 )) && print "  (none)"

if (( n_orphan > 0 )); then
  print ""
  print "=============================================================="
  print "No matching original found — probably just named this way ($n_orphan)"
  print "=============================================================="
  for f in $orphans; do print "  ${f#$ROOT/}"; done
fi

if (( n_skip > 0 )); then
  print ""
  print "=============================================================="
  print "Not downloaded from iCloud, could not compare ($n_skip)"
  print "=============================================================="
  for f in $skipped; do print "  ${f#$ROOT/}"; done
fi

# -------------------------------------------------------------- quarantine --
if (( QUARANTINE && n_ident > 0 )); then
  print ""
  print "Moving $n_ident identical duplicate(s) to $QDIR ..."
  i=0
  for f in $identical; do
    (( i++ ))
    draw_bar $i $n_ident "${f:t}"
    rel="${f#$ROOT/}"
    dest="$QDIR/$rel"
    mkdir -p "${dest:h}"
    mv -n "$f" "$dest" && (( n_moved++ ))
  done
  clear_bar
  print "Moved $n_moved file(s). Review $QDIR, then drag it to the Trash."
elif (( n_ident > 0 )); then
  print ""
  print "Report only. Re-run with --quarantine to move the identical ones aside."
fi

print ""
print "Done. $total candidate(s) examined."