#!/usr/bin/env bash
# #271 — add the date_dnf column to the `read` table (DNF / Did Not Finish).
# Backs the DB up first (Jan-2026 data-loss incident rule), then ALTERs.
# Idempotent: no-ops if the column already exists.
set -euo pipefail

DB="/home/brandon/projects/GreatReads/greatreads/data/greatreads.db"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${DB}.bak-271-${STAMP}"

echo "Backing up ${DB} -> ${BACKUP}"
cp -a "$DB" "$BACKUP"

if sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('read') WHERE name='date_dnf';" | grep -q '^0$'; then
  echo "Adding column read.date_dnf ..."
  sqlite3 "$DB" "ALTER TABLE read ADD COLUMN date_dnf DATE;"
  echo "Done."
else
  echo "Column read.date_dnf already exists — nothing to do."
fi

echo "Verify:"
sqlite3 "$DB" "SELECT COUNT(*) AS has_date_dnf FROM pragma_table_info('read') WHERE name='date_dnf';"
