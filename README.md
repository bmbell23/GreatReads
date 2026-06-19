# GreatReads

A self-hosted reading platform: a personal **library / TBR / reading-journal / stats**
app fused with an **e-reader + audiobook player** that sit on top of your own
[Calibre](https://calibre-ebook.com/) and [Audiobookshelf](https://www.audiobookshelf.org/)
libraries. Read ebooks in the browser, listen to audiobooks, track progress (synced across
formats by chapter), highlight, and keep stats — all from your own server.

This is the canonical, production repo. It's the result of merging the former standalone
GreatReads app and the standalone "Ereader" backend into one codebase
(see [`greatreads/VENDORED_FROM.md`](greatreads/VENDORED_FROM.md) and GitHub issues
[#22](https://github.com/bmbell23/GreatReads/issues/22) / the merge "stories").

> **Source of truth for work:** plans, scope, and status live in **GitHub Issues**, not in
> markdown files. See [`CLAUDE.md`](CLAUDE.md) for the working rules. This README describes
> *what runs and how*; the issues describe *what's next*.

## Architecture

Two processes serve the app:

| Port | What | How it runs | Source |
|------|------|-------------|--------|
| **`:8092`** | **Unified FastAPI app** — GreatReads (library/TBR/journal/stats + the canonical SQLite DB) **plus** the absorbed Ereader routes (`/api/catalog`, `/api/ebooks`, audiobooks, highlights, progress, summaries). | Docker container `greatreads_ereader` (`restart: unless-stopped` + healthcheck). | [`greatreads/`](greatreads/) (code in [`greatreads/src/greatreads/`](greatreads/src/greatreads/); Ereader routes in [`ereader_api.py`](greatreads/src/greatreads/ereader_api.py)) |
| **`:8090`** | Static reader files (`reader.html`, `player.html`, …) + a `/greatreads/` reverse proxy to `:8092`. | Bare-metal `python web/serve.py`. | [`web/`](web/) |

The container reaches the host's Calibre (`:8083`) and Audiobookshelf (`:13378`) over
`host.docker.internal`, and mounts both libraries **read-only** for cover/metadata fallback.

> **Retired:** the old standalone backend on `:8091` ([`backend/`](backend/)) and the old
> prod GreatReads on `:8007`. `backend/app.py` is kept only as rollback (see `CLAUDE.md`).
> Don't restart `:8091` — debug Ereader features on `:8092`.

### Data

- **Canonical DB + covers:** `greatreads/data/greatreads.db` — a bind mount
  (`./data:/app/data`), **never** baked into the image and **gitignored**.
- Code is baked into the image (`COPY src`), so **code changes need a rebuild**; data
  survives rebuilds untouched.
- ⚠️ **Back up `greatreads/data/greatreads.db` before any schema/data migration** (there was
  a Jan-2026 data-loss incident — treat the DB with care). Code-only rebuilds need no backup.

## Running it

Build & (re)start the unified container — this is data-safe (DB lives in the bind mount):

```bash
docker compose -p greatreads_ereader -f greatreads/docker-compose.ereader.yml up -d --build
```

Health check (expect `calibre_connected: true`):

```bash
curl -sf localhost:8092/api/health
docker ps --filter name=greatreads_ereader      # expect "Up ... (healthy)"
```

The static server on `:8090` runs bare-metal:

```bash
python web/serve.py
```

### Secrets

The container reads `greatreads/.ereader.env` (gitignored) for `ABS_TOKEN`. All other config
(ports, library paths, public host/URLs) is in
[`greatreads/docker-compose.ereader.yml`](greatreads/docker-compose.ereader.yml).

### Re-staging baked assets (gotcha)

The Docker build context is `greatreads/`, so it can't `COPY` from `backend/` or the repo
root. Baked copies live under `greatreads/ereader-assets/`. If you edit
`backend/summaries/*` or bump `version.txt`, **re-stage before rebuilding** or the container
serves stale data:

```bash
cp backend/summaries/*.json greatreads/ereader-assets/summaries/
cp version.txt greatreads/ereader-assets/version.txt
```

## Android app

A native Android client lives in [`simple-app/`](simple-app/) (Gradle). Build and stage the
debug APK so a device can pull it from `http://<host>:8090/ereader.apk`:

```bash
./build-app.sh            # build + stage to web/ereader.apk
./build-app.sh --clean    # gradle clean first
```

## Repo layout

```
greatreads/    Unified FastAPI app — the :8092 container (Dockerfile, src/, migrations/, data/)
web/           Static reader/player + serve.py (the :8090 server)
backend/       Retired :8091 backend — kept as rollback only
simple-app/    Native Android client (Gradle) — built by build-app.sh
app/           Older Capacitor/Android scaffold
docs/          Repo docs (e.g. RECOVERY.md)
CLAUDE.md      Working rules + runtime orientation (read this)
```

## Troubleshooting

If audiobooks, covers, downloads, highlights, or progress break, it's the `:8092` container
(not `:8091`). Check it's healthy and that `calibre_connected` is true:

```bash
docker ps --filter name=greatreads_ereader
curl -sf localhost:8092/api/health
# if calibre_connected:false — verify host.docker.internal resolves:
docker exec greatreads_ereader curl -sf http://host.docker.internal:8083/ajax/library-info
```

See [`CLAUDE.md`](CLAUDE.md) for full runtime orientation and rollback steps, and
[`docs/RECOVERY.md`](docs/RECOVERY.md) for recovery procedures.
