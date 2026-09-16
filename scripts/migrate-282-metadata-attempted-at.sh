#!/usr/bin/env bash
# #282 — add books.metadata_attempted_at (backfill progress cursor).
# Backs the DB up first (Jan-2026 data-loss incident rule), then ALTERs.
# Idempotent: no-ops if the column already exists.
set -euo pipefail

DB="/home/brandon/projects/GreatReads/greatreads/data/greatreads.db"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${DB}.bak-282-${STAMP}"

echo "Backing up ${DB} -> ${BACKUP}"
cp -a "$DB" "$BACKUP"

if sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('books') WHERE name='metadata_attempted_at';" | grep -q '^0$'; then
  echo "Adding column books.metadata_attempted_at ..."
  sqlite3 "$DB" "ALTER TABLE books ADD COLUMN metadata_attempted_at DATETIME;"
  echo "Done."
else
  echo "Column books.metadata_attempted_at already exists — nothing to do."
fi

echo "Verify:"
sqlite3 "$DB" "SELECT COUNT(*) AS has_col FROM pragma_table_info('books') WHERE name='metadata_attempted_at';"
