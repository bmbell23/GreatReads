#!/usr/bin/env bash
# #90 — Purge the legacy "MISSING BOOK COVER" boilerplate placeholder.
# All boilerplate covers are byte-identical (md5 0aa062cafaa0e71b9b2cb03e6ed4a73a).
# For real book rows: clear the cover flag + delete the image so they fall back to
# the generated title/author placeholder. For orphan cover files (no book row):
# just delete the file.
#
# Usage:
#   scripts/purge_boilerplate_covers.sh           # dry run (shows what it would do)
#   scripts/purge_boilerplate_covers.sh --apply    # back up DB, then apply
set -euo pipefail

DATA="$(cd "$(dirname "$0")/../data" && pwd)"
DB="$DATA/greatreads.db"
COVERS="$DATA/covers"
THUMBS="$DATA/covers_thumb"
HASH="0aa062cafaa0e71b9b2cb03e6ed4a73a"
APPLY="${1:-}"

cd "$COVERS"
ids=$(md5sum ./*.jpg | awk -v h="$HASH" '$1==h{print $2}' | sed 's|\./||;s|\.jpg$||' | sort -n)
[ -z "$ids" ] && { echo "No boilerplate covers found."; exit 0; }

with_row=""; orphans=""
for id in $ids; do
  if [ "$(sqlite3 "$DB" "SELECT COUNT(*) FROM books WHERE id=$id;")" -gt 0 ]; then
    with_row="$with_row $id"
  else
    orphans="$orphans $id"
  fi
done
csv=$(echo $with_row | tr ' ' ',')

echo "Boilerplate covers: $(echo $ids | wc -w) total"
echo "  book rows to clear (cover=0 + delete file):$with_row"
echo "  orphan files to delete (no book row):$orphans"

if [ "$APPLY" != "--apply" ]; then
  echo
  echo "Dry run only. Re-run with --apply to back up the DB and execute."
  exit 0
fi

bak="$DB.bak-$(date +%Y%m%d-%H%M%S)"
cp "$DB" "$bak"
echo "DB backed up -> $bak"

sqlite3 "$DB" "UPDATE books SET cover=0 WHERE id IN ($csv);"
echo "Cleared cover flag on book rows."

for id in $ids; do
  rm -f "$COVERS/$id.jpg" "$THUMBS/$id.jpg"
done
echo "Deleted cover + thumbnail files. Done."
