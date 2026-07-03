#!/usr/bin/env bash
# Rebuild the greatreads_ereader (:8092) container, baking in build metadata (#180):
#   • BUILD_STAMP  — YYMMDD-HH:MM host clock at build time (→ /app/build-stamp.txt,
#                    shown on the hamburger "Built …" pill via /api/build-time).
#   • BUILD_DIRTY  — 'M' when the working tree has uncommitted tracked changes, else
#                    empty (→ Settings "App version" shows e.g. 1.2.195M via /api/version).
#
# The container has no git (.git lives outside the greatreads/ build context), so the
# dirty flag MUST be computed here on the host and passed in as a build-arg.
#
# This is the canonical way to rebuild the app — it just wraps the documented
# `docker compose … up -d --build` with those two args. Data-safe: the DB + covers
# live in the bind-mounted greatreads/data, never in the image.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

BUILD_STAMP="$(date '+%y%m%d-%H:%M')"

# Dirty = any uncommitted tracked change, EXCLUDING the auto-synced asset copy
# (greatreads/ereader-assets/version.txt is rewritten by the pre-commit hook and
# would otherwise trip a false 'M').
if [ -n "$(git status --porcelain | grep -v 'greatreads/ereader-assets/version.txt' || true)" ]; then
  BUILD_DIRTY="M"
else
  BUILD_DIRTY=""
fi

echo "Rebuilding greatreads_ereader  →  stamp=${BUILD_STAMP}  version=$(cat version.txt 2>/dev/null || echo '?')${BUILD_DIRTY:+ (modified: ${BUILD_DIRTY})}"

GREATREADS_BUILD_STAMP="$BUILD_STAMP" \
GREATREADS_BUILD_DIRTY="$BUILD_DIRTY" \
  docker compose -p greatreads_ereader -f greatreads/docker-compose.ereader.yml up -d --build "$@"
