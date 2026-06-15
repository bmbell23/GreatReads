#!/bin/bash
# Daily online backup of the canonical GreatReads SQLite DB (the :8092 instance,
# which is now production — Story 2). Uses SQLite's online `.backup`, which is
# consistent even while the app + Ereader backend hold the DB open (no need to
# stop the container). Keeps the last RETAIN backups and prunes older ones.
#
# Wire into cron (already added):
#   30 2 * * *  /home/brandon/projects/Ereader/greatreads/scripts/backup-db.sh >> .../backup.log 2>&1
set -euo pipefail

REPO="/home/brandon/projects/Ereader"
DB="$REPO/greatreads/data/greatreads.db"
DEST="$REPO/greatreads/data/backups"
RETAIN=14

mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/greatreads-$STAMP.db"

# Online, consistent copy (safe with the DB open / WAL).
sqlite3 "$DB" ".backup '$OUT'"

# Verify the copy before trusting it; delete it if it's corrupt.
if [ "$(sqlite3 "$OUT" 'PRAGMA integrity_check;')" != "ok" ]; then
    echo "$(date -Is) BACKUP FAILED integrity_check: $OUT" >&2
    rm -f "$OUT"
    exit 1
fi

echo "$(date -Is) backup ok: $OUT ($(stat -c %s "$OUT") bytes)"

# Prune: keep the newest RETAIN timestamped backups (never touch precutover/manual ones).
ls -1t "$DEST"/greatreads-[0-9]*.db 2>/dev/null | tail -n +$((RETAIN + 1)) | while read -r old; do
    rm -f "$old" && echo "$(date -Is) pruned old backup: $old"
done
