#!/bin/bash
# Keep the static web server alive with auto-restart on crash, AND keep the
# checkout up to date so merges to origin/main go live automatically.
#
# The Android app is a thin WebView pointed at this server, so "deploying" a
# change just means this box pulling origin/main — there is no build step. The
# watchdog now does that pull on a throttle (~every 60s) and restarts the
# static server whenever main actually advanced.
#
# Usage: ./keep-alive.sh &

cd "$(dirname "$0")"
REPO_ROOT="$(cd .. && pwd)"
LOG_FILE="/tmp/ereader-static.log"
PID_FILE="/tmp/ereader-static.pid"
PULL_BRANCH="main"
PULL_EVERY=12          # loop iterations between git pulls (5s each → ~60s)

# Kill any existing instances
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo "Stopping old server (PID $OLD_PID)"
        kill "$OLD_PID" 2>/dev/null
        sleep 1
    fi
fi

echo "Starting Ereader static server watchdog..."
echo "Log: $LOG_FILE"
echo "PID: $$" > "$PID_FILE"

# Fast-forward the checkout to origin/$PULL_BRANCH. Non-destructive: --ff-only
# never creates merge commits and never clobbers local edits — if the checkout
# has diverged the pull simply fails and is logged, leaving files untouched.
# Returns 0 only when HEAD actually moved (so the caller knows to restart).
deploy_latest() {
    git -C "$REPO_ROOT" rev-parse --git-dir > /dev/null 2>&1 || return 1
    local before after
    before=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)
    git -C "$REPO_ROOT" pull --ff-only origin "$PULL_BRANCH" >> "$LOG_FILE" 2>&1
    after=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)
    if [ -n "$after" ] && [ "$before" != "$after" ]; then
        echo "[$(date)] Deployed $PULL_BRANCH: ${before:0:9} → ${after:0:9}"
        return 0
    fi
    return 1
}

pull_count=0
while true; do
    # --- Auto-deploy: pull origin/main on a throttle, restart on change ---
    if [ "$pull_count" -le 0 ]; then
        pull_count=$PULL_EVERY
        if deploy_latest; then
            # New code on disk — bounce the static server so any serve.py
            # change takes effect (HTML/CSS/JS are picked up on next request
            # regardless, but a clean restart keeps everything consistent).
            echo "[$(date)] Restarting static server after deploy"
            pkill -f "python3 serve.py" 2>/dev/null
            sleep 1
        fi
    fi
    pull_count=$((pull_count - 1))

    # Check if server is running on port 8090
    if ! ss -ltn | grep -q ":8090 "; then
        echo "[$(date)] Server not found on port 8090, starting..."

        # Clean up any stale processes
        pkill -f "python3 serve.py" 2>/dev/null
        sleep 1

        # Start the server
        python3 serve.py >> "$LOG_FILE" 2>&1 &
        SERVER_PID=$!

        echo "[$(date)] Started server with PID $SERVER_PID"
        sleep 2

        # Verify it started
        if ss -ltn | grep -q ":8090 "; then
            echo "[$(date)] Server is UP on port 8090"
        else
            echo "[$(date)] ERROR: Server failed to start!"
            tail -20 "$LOG_FILE"
        fi
    fi

    # Check every 5 seconds
    sleep 5
done
