#!/bin/bash

# Example usage:
# export CALIBRE_URL="http://localhost:8083"
# export CALIBRE_LIBRARY="library"
# ./run.sh
#
# Audiobookshelf (optional): put ABS_TOKEN (and optionally ABS_URL /
# ABS_LIBRARY_ID) in backend/abs.env — it's sourced below and gitignored so
# the token never lands in the repo. Without it, every ABS path no-ops and the
# library falls back to Calibre-only.

# Use defaults if not set
if [ -z "$CALIBRE_URL" ]; then
    export CALIBRE_URL="http://localhost:8083"
    echo "Using default CALIBRE_URL: $CALIBRE_URL"
fi

if [ -z "$CALIBRE_LIBRARY" ]; then
    export CALIBRE_LIBRARY="library"
    echo "Using default CALIBRE_LIBRARY: $CALIBRE_LIBRARY"
fi

# Load optional Audiobookshelf credentials (token etc.) from abs.env.
if [ -f "abs.env" ]; then
    set -a
    source abs.env
    set +a
    echo "Loaded abs.env"
fi

# Default ABS_URL to the local audiobookshelf container when a token is set
# but no URL was provided.
if [ -n "$ABS_TOKEN" ] && [ -z "$ABS_URL" ]; then
    export ABS_URL="http://localhost:13378"
    echo "Using default ABS_URL: $ABS_URL"
fi

echo "Starting Ereader Backend Server..."
echo "Calibre URL: $CALIBRE_URL"
echo "Calibre Library: $CALIBRE_LIBRARY"
if [ -n "$ABS_TOKEN" ]; then
    echo "Audiobookshelf: enabled (${ABS_URL})"
else
    echo "Audiobookshelf: disabled (no ABS_TOKEN)"
fi

# Install dependencies if needed
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

# FastAPI app (app.py) served by uvicorn. --reload keeps the auto-deploy
# behavior the old Flask reloader provided: keep-alive.sh fast-forwards the
# checkout and uvicorn restarts when app.py changes on disk. The Werkzeug
# debug server (an RCE risk on a LAN-listening port) is gone entirely.
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8091}" --reload
