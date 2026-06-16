# Ereader Recovery Checklist

## When Nothing Loads (Black Screen / Empty Library)

### 1. Check Both Services Are Running

```bash
# Backend (port 8091)
curl -s http://localhost:8091/api/health

# Static server (port 8090)
ss -ltnp | grep :8090

# Quick library test
curl -s "http://localhost:8091/api/library?limit=2" | jq '.books[0].title'
```

### 2. Restart Backend if Down

```bash
# Kill existing backend (if PID file exists)
kill $(cat /home/brandon/projects/Ereader/server.pid) 2>/dev/null || true

# Or find and kill manually
ps aux | grep "backend/server.py" | grep -v grep
kill <PID>

# Start backend (creates new server.pid)
cd /home/brandon/projects/Ereader/backend
./run.sh &

# Wait for startup
sleep 3

# Verify it's healthy
curl -s http://localhost:8091/api/health
# Should return: {"status": "ok", "calibre_connected": true, ...}
```

### 3. Restart Static Server if Down

**The static server now has a watchdog (`web/keep-alive.sh`) that auto-restarts it on crash.**

Check if watchdog is running:
```bash
ps aux | grep keep-alive | grep -v grep
# Should show: /bin/bash ./keep-alive.sh
```

If watchdog is NOT running, start it:
```bash
cd /home/brandon/projects/Ereader/web
nohup ./keep-alive.sh > /tmp/ereader-watchdog.log 2>&1 &

# Check logs
tail -f /tmp/ereader-watchdog.log
# Should show: "Server is UP on port 8090"
```

If watchdog IS running but server still down:
```bash
# Check watchdog logs for errors
cat /tmp/ereader-watchdog.log

# Manual restart (watchdog will take over after this)
pkill -f "python3 serve.py"
sleep 6
ss -ltnp | grep :8090
```

### 4. Force-Close the App

**CRITICAL**: The Android WebView aggressively caches HTML/JS. After ANY code change:

1. Swipe app away from recent apps (don't just back out)
2. Wait 2 seconds
3. Reopen app

Simply navigating back to the library is NOT enough - the WebView keeps cached JS in memory.

### 5. Check Git Status

```bash
cd /home/brandon/projects/Ereader
git status --short
git diff <file>
```

If things are broken and you don't know why: `git checkout .` to revert everything.

### 6. Verify Network Connectivity

**From the server (via Tailscale IP - what the phone sees):**

```bash
# Backend reachable from Tailscale IP?
curl -s http://100.69.184.113:8091/api/health
# Should return JSON with "status": "ok"

# Static server reachable?
curl -s http://100.69.184.113:8090/ | head -10
# Should return HTML starting with "<!DOCTYPE html>"

# Full test: fetch library
curl -s "http://100.69.184.113:8091/api/library?limit=1" | jq '.books[0].title'
# Should return a book title
```

**If these fail but localhost works:**
- Tailscale may be down: `sudo tailscale status`
- Firewall blocking: Check `iptables` rules
- Services bound to wrong interface: Should bind to `0.0.0.0`, not `127.0.0.1`

## Common Mistakes That Break Everything

1. **JavaScript syntax errors** - The entire page goes black. No error visible to user.
2. **Calling undefined functions** - Variables/functions used before they're defined.
3. **Uncaught Promise rejections** - Async functions without `.catch()` can break initialization.
4. **Using unsupported APIs** - `AbortSignal.timeout()` doesn't work in older WebViews.
5. **Forgetting to restart services** - Code changes to backend require a restart.
6. **Not force-closing the app** - WebView cache is VERY sticky.

## Safe Development Workflow

1. **Make small changes** - One feature at a time, test immediately.
2. **Check syntax** - `node -e "require('fs').readFileSync('web/index.html')"` catches gross errors.
3. **Test backend changes** - `curl` the endpoint before touching frontend.
4. **Keep a working baseline** - `git commit` frequently so you can revert.
5. **Read server logs** - Backend logs go to `backend/server.log`.
6. **Never edit both frontend and backend simultaneously** - Change one, test, then change the other.
7. **ALWAYS force-close the app** - After ANY frontend change, swipe away and reopen. Back button is not enough.

## Emergency Recovery Command

```bash
cd /home/brandon/projects/Ereader

# Revert all code changes
git checkout .

# Kill backend
kill $(cat server.pid) 2>/dev/null || true

# Kill static server + watchdog
pkill -f "keep-alive.sh"
pkill -f "python3 serve.py"
sleep 1

# Restart backend
cd backend && ./run.sh &
sleep 2

# Start static server watchdog (auto-restarts on crash)
cd ../web && nohup ./keep-alive.sh > /tmp/ereader-watchdog.log 2>&1 &
sleep 3

# Verify both are up
echo "Checking services..."
curl -s http://localhost:8091/api/health | jq -r '.status' && echo "✓ Backend OK" || echo "✗ Backend FAILED"
curl -s http://100.69.184.113:8090/ | head -1 && echo "✓ Static server OK" || echo "✗ Static server FAILED"
ps aux | grep keep-alive | grep -v grep && echo "✓ Watchdog running" || echo "✗ Watchdog NOT running"
```

**Then on your phone:**
1. Swipe the app away from recent apps (complete force-close)
2. Wait 3 seconds
3. Reopen the app
4. Library should load normally
