# GreatReads Merge — Execution Scoping (file-level)

Companion to [`GREATREADS_MERGE_PLAN.md`](GREATREADS_MERGE_PLAN.md). That doc holds the
strategy and the Jira **Stories**; this doc holds the **how** — exact files, commands, and
code-change locations to implement each Story. Deep detail for Stories 0–2 (next up), lighter
for 3–5 (further out, revisit when we get there).

> **All shell/docker commands below are PROPOSED — none have been run.** No code changed yet.

---

## ⚠️ Production-safety guardrails (read first)

The host already runs **production GreatReads** as Docker container `greatreads_app`
(`8007→8006`), and its `docker-compose.yml` carries a loud warning about a Jan-7-2026
data-loss incident. Therefore:

- **Never** `docker-compose down` or restart the whole stack; operate on individual
  containers only.
- **Never** point our work at the production data dir `GreatReads/data/` or the
  `greatreads_app` container. We use an **isolated copy** of the DB and a **separate
  container/port**.
- The Calibre/ABS mounts are **read-only** (`:ro`) — keep them that way.
- The live `greatreads.db` uses WAL (`-wal`/`-shm` present); copy it **checkpointed** or while
  idle so the copy is consistent.

Verified environment facts (2026-06-14):
- `greatreads_app` container: host `:8007` → container `:8006`.
- Calibre library on disk: `/home/brandon/projects/docker/calibre/config/library`.
- ABS data on disk: `/home/brandon/projects/docker/audiobookshelf/data` (compose mount).
- Free host port for our instance: **:8092**.
- Canonical DB to copy: `GreatReads/data/greatreads.db` (593 KB, active WAL) + `covers/`.

---

## Story 0 — Vendor GreatReads & run it isolated on :8092

> **STATUS: ✅ DONE (2026-06-14).** Vendored from commit `dbafbc1` into `greatreads/`; isolated
> container `greatreads_ereader` runs on **:8092** (healthy), serving a `VACUUM INTO` copy of
> prod data (books 1516 / readings 946, `brandon` user + 1200 external_imports present).
> Schedulers off (`ENABLE_SCHEDULERS=false` verified). Production `greatreads_app` (:8007) and
> Ereader (:8090/:8091) untouched. Deviations from the plan below: used `VACUUM INTO` (not
> checkpoint+cp) so prod is never written; the user table is `users` (plural).
> Run: `docker compose -p greatreads_ereader -f greatreads/docker-compose.ereader.yml up -d --build`

### Deployment decision: **run it as a second, isolated Docker container** (recommended)
GreatReads is *built for Docker* — its code hardcodes container paths (`main.py`
`cover_thumbnail` → `/app/data/covers`; `config.py` `covers_dir`/`database_url` branch on
`is_docker`). Running it bare-metal means patching those paths. Running our own container
reuses its Dockerfile, gets the right paths for free, and stays fully isolated from both the
working Ereader app **and** production GreatReads. (Bare-metal uvicorn is the form we converge
to in **Story 4** when everything merges into one FastAPI process — not now.)

### Vendor layout (OQ1: plain copy, not submodule)
Proposed new directory in this repo:
```
greatreads/                         # vendored from GreatReads @ commit dbafbc1 (v2.1.7)
  VENDORED_FROM.md                  # records source commit + date + "do not edit upstream"
  src/greatreads/...                # copied app code (FastAPI, templates, static, models…)
  migrations/                       # copied
  scripts/                          # copied (merge-duplicates, user setup…)
  Dockerfile                        # copied
  requirements.txt                  # frozen from its pyproject.toml
  data/                             # GITIGNORED live data (copied DB + covers); see below
  docker-compose.ereader.yml        # NEW — our isolated service (below)
```
`.gitignore` additions: `greatreads/data/*.db`, `greatreads/data/*.db-*`,
`greatreads/data/covers*/`, any venv. Decide later whether to commit a small seed DB.

### One required code change: a scheduler kill-switch
`src/greatreads/main.py` `lifespan()` unconditionally starts APScheduler (midnight chain
recalc + 15-min Calibre/ABS auto-sync; lines 92–124). For our isolated instance we want them
**off** until Story 2 (OQ6) so nothing writes our copy DB behind our back. Add an env gate:

```python
# main.py lifespan(), wrapping the scheduler block
import os
if os.environ.get("ENABLE_SCHEDULERS", "true").lower() == "true":
    ...existing scheduler setup...
```
Set `ENABLE_SCHEDULERS=false` in our compose for Stories 0–1; flip to `true` in Story 2.

### Proposed `greatreads/docker-compose.ereader.yml`
```yaml
services:
  greatreads_ereader:                 # distinct name — NOT greatreads_app
    build: .
    container_name: greatreads_ereader
    restart: unless-stopped
    ports:
      - "8092:8006"                   # our free host port
    volumes:
      - ./data:/app/data              # repo-local COPY of the DB + covers (never prod ./data)
      - /home/brandon/projects/docker/calibre/config/library:/calibre:ro
      - /home/brandon/projects/docker/audiobookshelf/data:/audiobookshelf:ro
    environment:
      - HOST=0.0.0.0
      - PORT=8006
      - DEBUG=false
      - TZ=America/Denver
      - DATABASE_URL=sqlite:////app/data/greatreads.db
      - APP_PATH=/greatreads          # matches the reverse-proxy prefix (Story 1)
      - ENABLE_SCHEDULERS=false        # Stories 0–1; flip true in Story 2
      - SECRET_KEY=${SECRET_KEY:-local-dev-not-secret}
      - CALIBRE_DB_PATH=/calibre/metadata.db
      - CALIBRE_LIBRARY_PATH=/calibre
      - ABS_DB_PATH=/audiobookshelf/absdatabase.sqlite
      - ABS_METADATA_PATH=/audiobookshelf/metadata
    networks: [greatreads_ereader_net]
networks:
  greatreads_ereader_net: { driver: bridge }
```
Use an explicit compose **project name** (`-p greatreads_ereader`) so it never collides with
the production stack.

### DB provisioning (proposed, isolated)
```bash
# Copy a CHECKPOINTED, consistent snapshot of prod data into the repo-local copy.
mkdir -p greatreads/data
sqlite3 /home/brandon/projects/GreatReads/data/greatreads.db "PRAGMA wal_checkpoint(TRUNCATE);"
cp /home/brandon/projects/GreatReads/data/greatreads.db greatreads/data/greatreads.db
cp -r /home/brandon/projects/GreatReads/data/covers       greatreads/data/covers
cp -r /home/brandon/projects/GreatReads/data/covers_thumb greatreads/data/covers_thumb
```

### Launch + process management
- Build & run: `docker compose -p greatreads_ereader -f greatreads/docker-compose.ereader.yml up -d --build`.
- Integrate into whatever launches `:8090`/`:8091` today (the same supervisor/tmux/systemd).
  GreatReads ships `config/systemd` examples if we want a unit.

### Acceptance / verification
```bash
curl -fs http://127.0.0.1:8092/health        # → {"status":"ok",...}
curl -fsI http://127.0.0.1:8092/ | head        # → 200, GreatReads home HTML
docker ps --format '{{.Names}} {{.Ports}}' | grep greatreads   # both app + ereader, distinct ports
```
- Ereader `:8090`/`:8091` unchanged & working. Production `greatreads_app` untouched.

---

## Story 1 — "GreatReads" button (reverse-proxy + menu entry)

> **STATUS: ✅ DONE (2026-06-14).** `web/serve.py` reverse-proxies `/greatreads/*` → `:8092`
> (prefix-strip + `X-Forwarded-Prefix`, forwards original `Host` so `url_for()` links resolve
> to the client origin, streams responses). Button added at `index.html` `#menu-popup`
> (`📚 GreatReads` → `/greatreads/`). Auth needs nothing — GreatReads auto-logs-in as `brandon`,
> and the client is already prefix-aware (`window.APP_BASE_PATH`). Verified live on :8090:
> home/css/api 200, `/greatreads`→301, no `:8092` leak, reader/player/api intact.
> Restart model: `:8090` serve.py is bounced by `keep-alive.sh`; `:8091` Flask hot-reloads
> (`debug=True`) — do **not** kill it (`run.sh` won't relaunch it).
>
> **Also (Story 2 brought forward per request): progress dual-write.** `backend/server.py`
> mirrors every GreatReads write (sync %, finish ratings, start-next) to a best-effort
> `GREATREADS_MIRROR_URLS` (default `http://127.0.0.1:8092`) while the primary `GREATREADS_URL`
> stays prod `:8007`. → progress now reaches BOTH. To go local-only later: set
> `GREATREADS_URL=http://127.0.0.1:8092` + `GREATREADS_MIRROR_URLS=""`.

### 1a. Reverse proxy in `web/serve.py`
`serve.py` is a 95-line `ThreadingHTTPServer` + `SimpleHTTPRequestHandler` (no proxy today).
Its `end_headers()` force-injects `no-store` + CORS + APK headers on **every** response — we
must **not** route proxied responses through that path, or we'll clobber GreatReads' own
headers/redirects. Plan: intercept early in `do_GET`/`do_POST`/`do_PUT`/`do_DELETE`/`do_HEAD`,
and for paths starting `/greatreads`, write the response manually and `return` before the
static handler runs.

Proposed shape (added to the handler class):
```python
UPSTREAM = "127.0.0.1:8092"

def _proxy_greatreads(self):
    import http.client
    # strip nothing: GreatReads is prefix-aware via X-Forwarded-Prefix
    conn = http.client.HTTPConnection(UPSTREAM, timeout=30)
    body = None
    if "Content-Length" in self.headers:
        body = self.rfile.read(int(self.headers["Content-Length"]))
    fwd = {k: v for k, v in self.headers.items() if k.lower() != "host"}
    fwd["X-Forwarded-Prefix"] = "/greatreads"            # makes url_for() emit /greatreads/...
    fwd["X-Forwarded-Host"]   = self.headers.get("Host", "")
    conn.request(self.command, self.path, body=body, headers=fwd)
    up = conn.getresponse()
    self.send_response(up.status)
    for k, v in up.getheaders():
        if k.lower() in ("transfer-encoding", "connection"):
            continue
        # keep Set-Cookie, Content-Type, Location (already /greatreads-prefixed), Cache-Control
        self.send_header(k, v)
    self.end_headers_raw()                                # bypass the no-store injector
    while True:                                            # stream (covers, css, big bodies)
        chunk = up.read(64 * 1024)
        if not chunk: break
        self.wfile.write(chunk)
    conn.close()
```
Implementation notes / gotchas:
- Add an `end_headers_raw()` (call `BaseHTTPRequestHandler.end_headers` directly) so the proxy
  branch skips the no-cache/CORS/APK injection.
- Override each `do_*` to call `self._proxy_greatreads()` when
  `self.path.split("?")[0].startswith("/greatreads")`, else fall through to `super().do_GET()`.
- `Location` redirects from GreatReads are already `/greatreads`-prefixed (because of
  `X-Forwarded-Prefix`), so no rewriting needed. Verify with the login/redirect paths.
- `Set-Cookie` passes through unchanged → same-origin cookie works (and auth is auto-login
  anyway, see 1b).
- No WebSockets in GreatReads, so a plain HTTP proxy is sufficient.
- It's already `ThreadingMixIn`, so a slow proxied stream won't block other clients.
- **Fallback:** if the Python proxy is fragile under load/streaming, drop in **nginx**
  (GreatReads ships `greatreads_nginx_config.txt`) in front instead; the button URL
  (`/greatreads/`) stays identical.

### 1b. Auth — already handled (verify only)
`src/greatreads/auth.py` `get_current_user_from_cookie()` **auto-returns the `brandon` user
when no cookie is present** (lines ~55–75). So there is no login wall. Tasks reduce to:
- Confirm the copied DB contains the `brandon` user (it's a prod copy → yes).
- Confirm no page route hard-redirects to `/login` for the auto-user (spot-check `/`, `/tbr`,
  `/library`). No code change expected.

### 1c. Home-page button in `web/index.html`
The hamburger menu is `#menu-popup` at [index.html:634](web/index.html#L634):
```html
<div class="menu-popup" id="menu-popup">
    <button id="menu-highlights">✏️ Highlights</button>
    <button id="menu-bookmarks">🔖 Bookmarks</button>
    <button id="menu-requests">📋 Requests</button>
    <button id="menu-settings">⚙️ Settings</button>
    <button id="menu-about">ℹ️ About</button>
</div>
```
Add one item + handler (other items navigate via `location.href`; mirror that):
```html
<button id="menu-greatreads">📚 GreatReads</button>
```
```js
document.getElementById('menu-greatreads')
        .addEventListener('click', () => { location.href = '/greatreads/'; });
```
Optional later: a prominent home tile instead of (or in addition to) the menu item.

### 1d. Android
Because we serve same-origin under `:8090/greatreads/`, the existing WebView wrapper loads it
with **no APK change**. Back navigation uses the WebView's existing history handling.

### Acceptance / verification
- Browser: `http://<host>:8090/greatreads/` renders GreatReads with working CSS/JS, nav, and
  covers (proxied), no `/login` redirect.
- In the Android app: menu → GreatReads opens it; back returns to the library.
- Smoke-test reader, player, highlights, progress — all unaffected (only `index.html` +
  `serve.py` touched; `server.py` and data files untouched).

---

## Story 2 — Cross-link + retire remote :8007  ·  ✅ DONE (2026-06-15)

> Implemented: `GREATREADS_URL` default changed to `http://127.0.0.1:8092` and the dual-write
> mirror (`GREATREADS_MIRROR_URLS` / `_gr_mirror`) removed; `greatreads_app` (:8007) stopped (data
> kept); schedulers enabled on :8092 (`ENABLE_SCHEDULERS=true`); daily DB backups via
> `greatreads/scripts/backup-db.sh` (cron). Cross-side deep links remain a follow-up.

### 2a. Repoint Ereader → local service (near-trivial)
`backend/server.py:53`:
```python
GREATREADS_URL = os.environ.get('GREATREADS_URL', 'http://100.69.184.113:8007').rstrip('/')
```
All GreatReads calls funnel through this one constant (sync/finish/start-next/format use
`/api/readings/…`, incl. `/api/readings/tbr` at line 2249). Repoint by setting
`GREATREADS_URL=http://127.0.0.1:8092` in `backend/run.sh` (preferred) or changing the default.
The vendored `:8092` runs identical code, so all request/response shapes match — **no logic
changes** to the four `/api/greatreads/*` handlers.

### 2b. Identity helper (Calibre/ABS id → GreatReads book_id)
Add a helper (in `backend/server.py` for now; moves to FastAPI in Story 4) that queries the
vendored service for the `external_imports` mapping, or reads it directly. Shape:
`resolve_book_id(source, external_id) -> book_id | None` where `source ∈ {calibre,
audiobookshelf}` and `external_id` is the Calibre int / ABS uuid. Used for deep links (2c).
> Note: there is no public `external_imports` read endpoint yet — Story 2 either adds a small
> `GET /api/import/lookup?source=&external_id=` route to the vendored app, or resolves via the
> existing books/readings search by title as the current `/api/greatreads/format` flow does.

### 2c. Deep links (both directions)
- Ereader book/library card → "Open in GreatReads" → `/greatreads/...book_id` (via 2b).
- GreatReads book → "Read"/"Listen" → `reader.html?id=<calibre_id>` / `player.html?id=abs:<uuid>`
  (reverse map). Requires small template edits in the vendored book views.

### 2d. Enable schedulers + decommission remote
- Flip `ENABLE_SCHEDULERS=true` on the `:8092` container; confirm midnight recalc + 15-min sync
  run against our copy DB only.
- Prove parity with prod, then **stop** `greatreads_app` (`docker stop greatreads_app` —
  per safety rules, stop the single container; do not `down` the stack) and remove the
  `100.69.184.113:8007` references.
- Update `GREATREADS_INTEGRATION.md` to point local / mark superseded.

### Acceptance / verification
- `netstat`/logs show zero traffic to `:8007`; finish/sync/format work against `:8092`.
- Deep links resolve both directions.

---

## Story 3 — Unify data (JSON → SQLite)

> **STATUS: ✅ progress slice DONE (2026-06-14).** Reading progress moved off
> `progress.json` into the GreatReads SQLite DB. `backend/server.py`:
> `_load_progress`/`_save_progress` now read/write an `ereader_progress` table in
> `greatreads/data/greatreads.db` (full record as a JSON blob; `progress.json` kept as a
> best-effort backup + fallback so the reader can't lose position). Legacy `progress.json`
> auto-migrates on first load (12 records migrated). New `_gr_set_current_percent()` writes
> `read.current_percent` (+ `current_percent_manual_override`, `date_progress_set`) for the
> precisely-resolved in-progress reading (via `external_imports` — **no title matching**) on
> every `PUT /api/progress`. The title-matching batch **sync is retired** (`/api/greatreads/sync`
> is now a no-op; the progress mirror is gone). Verified end-to-end: a page-turn PUT moved
> GreatReads' `current_percent` 28→50→28 and the reader's position data round-tripped intact.
> **Still TODO for the rest of Story 3:** highlights → SQLite; days_estimate/chain recalc on
> progress write (only affects projected dates, not the displayed %); eventually drop the JSON
> backup once proven. Finish/start-next still go via the API (primary prod :8007 + :8092 mirror)
> — that flips in Story 2.

### (original lighter scope, for the remaining pieces)

- **Models/migrations** (match `GreatReads/migrations/*.sql` style): add `progress`,
  `highlights`, optional `requests` tables to the vendored schema. Key rows to `books.id`,
  resolving Calibre/ABS-keyed data via `external_imports`.
- **One-time migration scripts** (idempotent, reversible from JSON backup) reading
  `backend/data/{progress,highlights,requests}.json`. Log unmatched books.
- **Dual-write window**: progress/highlight endpoints write both JSON + SQLite, compare for
  drift, cut reads to SQLite, then stop JSON writes. Keep JSON as backup.
- Source files to port off JSON: the progress/highlight handlers in `backend/server.py`
  (`/api/progress*`, `/api/highlights*`).

## Story 4 — One FastAPI backend; delete Flask  *(lighter scope)*

- Port `backend/server.py` routers into the vendored FastAPI app path-for-path. Hardest piece:
  the **audiobook HLS proxy + playback sessions** (chunked/streamed) — own sub-task + streaming
  tests. Also Calibre proxy, `/api/library|series|saga|summaries`, highlights, progress,
  requests, health/version/build-stamp.
- Serve reader/player static assets via FastAPI `StaticFiles`, preserving `no-store` semantics
  the WebView relies on (replicate `serve.py`'s headers).
- Delete `backend/server.py` + the `:8091` service; collapse to one venv/`pyproject.toml`. At
  this point the app can run **bare-metal** (no Docker) as one process, which is the end state.

## Story 5 — App-shaping  *(diagnosis already in PLAN §Android Readiness)*

- **5a (do first):** vendor CDN assets locally. Bake Bootstrap/FontAwesome/axios/SortableJS
  into `src/greatreads/static/vendor/` (download at build time in the Dockerfile, or commit
  them), and replace the 5 CDN `<link>/<script>` in `templates/base.html`. Removes the offline
  failure.
- 5b nav, 5c safe-area/manifest, 5d touch, 5e table→card reflow, 5f Sortable on-device, 5g
  theme unification, 5h Android bridge. See PLAN for the per-item mapping to diagnosis A1–A11.

---

## Quick reference — touch-point index
| Concern | File:line |
|---|---|
| Ereader → GreatReads base URL (Story 2 repoint) | `backend/server.py:53` |
| GreatReads finish/sync/format/start-next handlers | `backend/server.py:~2009–2350` |
| Static server to add proxy (Story 1) | `web/serve.py` (whole file, 95 ln) |
| Home menu to add button (Story 1) | `web/index.html:634` (`#menu-popup`) |
| Scheduler kill-switch to add (Story 0) | `GreatReads src/greatreads/main.py:92–124` |
| Auto-login (already done) | `GreatReads src/greatreads/auth.py:~55–75` |
| Config / env knobs | `GreatReads src/greatreads/config.py` |
| Prod compose (reference only — DO NOT REUSE its data dir) | `GreatReads/docker-compose.yml` |
| external_imports model (Story 2 identity bridge) | `GreatReads src/greatreads/models/external_import.py` |
