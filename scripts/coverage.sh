#!/usr/bin/env bash
# Run the greatreads test suite with branch coverage (#386).
# Usage: scripts/coverage.sh [extra pytest args]
# Needs the dev extras installed: pip install -e 'greatreads[dev]'
# Outputs: terminal report (term-missing), coverage.lcov, htmlcov/ (all in greatreads/).
set -euo pipefail

cd "$(dirname "$0")/../greatreads"

exec python3 -m pytest \
    --cov=greatreads \
    --cov-branch \
    --cov-report=term-missing \
    --cov-report=lcov:coverage.lcov \
    --cov-report=html:htmlcov \
    "$@"
