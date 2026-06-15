# Epic: Merge GreatReads into Ereader → one unified book app

**Status:** Planning (no code changes yet). Living doc — work spans multiple sessions.
**Last updated:** 2026-06-14
**Repo:** this one (Ereader). To be renamed **GreatReads** once the merge is real (§ Story 9).

**One-line goal:** Fold the GreatReads library/TBR manager into the Ereader reader/player so
we ship a single "book manager + reader + audiobook player" app, incrementally, **without
ever breaking the working Ereader app.**

> Related docs: **[`GREATREADS_MERGE_SCOPING.md`](GREATREADS_MERGE_SCOPING.md)** (file-level
> execution detail for each Story — read this when implementing),
> `GREATREADS_INTEGRATION.md` (the *current remote-sync* contract, to be retired in Story 2),
> `GREATREADS_CHAIN_REFERENCE.md` / `CHAIN_SYSTEM_ANALYSIS.md` (chain internals),
> `OFFLINE_PLAN.md` (offline thinking), `RECOVERY.md`.

---

## Architecture decisions (answers to the planning questions)

### D1 — Long-term framework: **FastAPI** (not Flask, not a rewrite in another language)
We converge the unified backend on **FastAPI**, and **delete Flask** at the end.
Why: GreatReads (~8,400 LOC) is already FastAPI — async, Pydantic validation, dependency
injection, auto OpenAPI docs, ASGI. Ereader's Flask server is the smaller, simpler piece
(~2,538 LOC) and is cheaper to port *forward* into FastAPI than the reverse. Both apps are
Python, so a non-Python rewrite (Node/Go/etc.) would throw away ~11k working LOC for no
real gain. **Decision: standardize on FastAPI.**

### D2 — We adopt a real database: **SQLite (via SQLAlchemy)**
Yes — the JSON-file approach is fragile (manual locking, no relations, no queries, no
migrations). We adopt GreatReads' existing **SQLite + SQLAlchemy** schema as the canonical
store and migrate Ereader's JSON state into it. SQLite is the right engine here: single-user,
file-based, zero-ops, already proven in GreatReads, and trivially backed up (it's one file).
(Postgres would be over-kill for a personal single-user app; revisit only if we ever go
multi-user/multi-device-write.)

### D3 — Yes, your Phase-3 mental model is correct (with one refinement)
Correct: we first bring GreatReads in as a **sidecar** (its own FastAPI process + its own
`greatreads.db`, running next to Ereader's untouched Flask). Once that SQLite DB lives *here*
and is healthy, migrating Ereader's `progress.json` / `highlights.json` into new tables in
**that same DB** is straightforward — same engine, same process boundary, and we already have
the `external_imports` table to join Ereader's Calibre/ABS IDs to GreatReads books.
Refinement: after Story 3 it stops being "the GreatReads sidecar's DB" and simply becomes
**the app's database**, shared by the reader and player too.

### D4 — "retire flash" = retire **Flask** (typo). Endgame = one backend.
Plan: Stories 0–2 run **two backends side by side** (Flask :8091 + vendored FastAPI :8092)
so nothing breaks. Story 3 unifies the data. Story 4 ports Ereader's Flask routes into the
FastAPI app and **deletes `backend/server.py` + the :8091 service**. End state: a single
FastAPI process serves every API. Flask is gone.

### Open-question resolutions (the 6 from the prior draft)
| # | Question | Decision |
|---|---|---|
| OQ1 | Vendor strategy | **Plain copy** of `src/greatreads/` into `greatreads/` in this repo (record source commit `dbafbc1`). Not a submodule — we will diverge heavily and the upstream repo gets archived. |
| OQ2 | Which DB is "our data" | Promote a **copy of production `GreatReads/data/greatreads.db`** (593 KB, actively used) as the repo-local canonical DB. `data-dev/` pattern stays available for local dev. |
| OQ3 | Auth | **Bypass / auto-login** for this personal single-user build (it already sits behind a Tailscale `100.x` address). Keep the `user`/auth code dormant, don't rip it out. |
| OQ4 | Proxy host | **Extend `web/serve.py`** with a reverse-proxy handler for `/greatreads/*` → `:8092` (same-origin for the WebView + cookies). It's currently a 95-line static `http.server`; the proxy must stream responses (covers, etc.). **nginx** (GreatReads ships a config) is the fallback if the Python proxy proves fragile, and the likely production choice later. |
| OQ5 | Port for the vendored service | **:8092 — confirmed free** on the host (8090/8091 = Ereader, 8083 = Calibre, 8096/8098/8099 also taken). |
| OQ6 | GreatReads background jobs (APScheduler) | **Off until Story 2.** The midnight chain-recalc and 15-min Calibre/ABS sync stay disabled while we stabilize, so nothing writes the DB behind our back. Enable deliberately in Story 2. |

---

## Current-state snapshot (for any session picking this up cold)

| | Ereader (this repo) | GreatReads (`../GreatReads`) |
|---|---|---|
| Framework | Flask sync, `backend/server.py` ~2,538 LOC, 43 routes, **:8091** | FastAPI async, `src/greatreads/` ~8,400 LOC, **:8007** |
| Storage | **JSON files** in `backend/data/` (no DB) | **SQLite** `data/greatreads.db` + SQLAlchemy, 9 tables |
| Frontend | Static HTML, inline CSS/JS, no build; `web/serve.py` **:8090** | Jinja2 + Bootstrap 5, assets from **CDN**; `url_for()`, honors `X-Forwarded-Prefix` |
| Packaging | Android WebView wrapper `simple-app/`, `build-app.sh`→`web/ereader.apk` | Docker, web-only |
| Identity | Calibre int IDs, `abs:<uuid>` for audio | own `book_id`; `external_imports(source, external_id)→book_id` bridges to Calibre/ABS |
| Bg jobs | none | APScheduler (midnight chain recalc, 15-min sync) |

The bridge that makes the whole merge tractable: **`external_imports`** already maps
Calibre/ABS IDs to GreatReads book IDs. GreatReads is also already mountable under a sub-path
(uses `url_for()` + `X-Forwarded-Prefix`), so it can live at `:8090/greatreads/` untouched.

---

## Target architecture (end state, after Story 5)

```
            Android app (WebView wrapper, simple-app/)  ── loads ──▶  web shell :8090
   ┌────────────────────────┬──────────────────────────┬──────────────────────────┐
 Reader UI (reader.html)  Player UI (player.html)   Library / TBR / Shelves / Stats
                                                     (vendored GreatReads pages, restyled)
                                   │ fetch()
                       ONE backend  ── FastAPI (this repo) ──
   reader/player/highlight/progress APIs (ported from Flask) + GR library/TBR/chains/
   shelves/stats/import + proxies to Calibre (:8083) & Audiobookshelf
                                   │
                       ONE SQLite DB (greatreads.db)
   books · read(chain) · inv · shelves · tags · settings · external_imports
   + progress · highlights · requests   (migrated out of JSON)
```

---

# Stories

Each phase below is written as a Jira **Story**: goal, scope, technical sub-tasks, acceptance
criteria, risk, and Definition of Done. Estimates are T-shirt sizes. Stories are sequential
unless noted; Stories 0+1 are the first shippable slice.

---

## Story 0 — Vendor GreatReads into the repo and stand it up headless
**Size:** M · **User-visible:** no · **Depends on:** none

**As** the maintainer **I want** GreatReads' code and data living inside this repo and running
as its own service **so that** all further work happens in one repo with no remote dependency.

> **Deployment decision (from scoping):** run it as an **isolated second Docker container**
> on :8092 (reuse its Dockerfile; its code hardcodes container paths like `/app/data`), with a
> **copied** DB and **read-only** Calibre/ABS mounts — never the production `greatreads_app`
> container or its data dir. Bare-metal is the Story-4 end state, not now. Full commands and
> the required scheduler kill-switch are in
> [`GREATREADS_MERGE_SCOPING.md`](GREATREADS_MERGE_SCOPING.md#story-0).

### Scope / sub-tasks
1. Copy `GreatReads/src/greatreads/`, `migrations/`, `pyproject.toml`, `scripts/`, templates
   and static assets into a new top-level `greatreads/` directory. Record the source commit
   (`dbafbc1 v2.1.7`) in `greatreads/VENDORED_FROM.md`.
2. Create an isolated Python env for it (separate venv / `greatreads/requirements.txt` frozen
   from its `pyproject.toml`) so Flask and FastAPI dependency trees never collide (OQ’s
   "separate venvs until Story 4").
3. Copy production `GreatReads/data/greatreads.db` → repo-local `greatreads/data/greatreads.db`
   (plus `covers/`, `covers_thumb/`). Confirm WAL checkpoint so the copy is consistent.
4. Configure it to bind **:8092** and to read the same Calibre (`:8083`) / Audiobookshelf
   endpoints the Flask server already uses.
5. **Disable APScheduler jobs** (midnight recalc + 15-min sync) via config/env for now (OQ6).
6. Add a run script (`greatreads/run.sh`) mirroring the existing `backend/run.sh` style;
   wire it into whatever process manager currently launches :8090/:8091.
7. Add `greatreads/data/*.db*`, covers, and venv to `.gitignore`; decide whether the seed DB
   is committed or provisioned (recommend: commit a small seed, gitignore the live DB).

### Acceptance criteria
- `GET http://<host>:8092/health` returns 200 from the vendored code.
- GreatReads pages render at `:8092` using the repo-local DB and show real data.
- Ereader (`:8090` web, `:8091` API) is **byte-for-byte unchanged** and fully functional.
- No process writes `greatreads.db` except the :8092 service (schedulers off).

### Risk → mitigation
- *Dependency collision* → separate venv. *DB copied mid-write* → checkpoint WAL / copy while
  source idle. *Port clash* → :8092 confirmed free. **Risk to Ereader: none (isolated process).**

### Definition of Done
Vendored service runs from this repo against repo-local data; Ereader untouched; committed.

---

## Story 1 — "GreatReads" button on the home page (first user-visible milestone)
**Size:** M · **User-visible:** yes · **Depends on:** Story 0

**As** a user **I want** a GreatReads button on the Ereader home screen **so that** I can open
the full GreatReads app (served from this repo, our data) and return to my library.

### Scope / sub-tasks
1. **Reverse-proxy** `/greatreads/*` in `web/serve.py` → `http://127.0.0.1:8092`, injecting
   `X-Forwarded-Prefix: /greatreads`. Implementation notes:
   - `serve.py` is a 95-line `ThreadingHTTPServer`/`SimpleHTTPRequestHandler`. Add a handler
     branch: if path starts with `/greatreads`, forward method + headers + body upstream and
     **stream** the response back (cover images, CSS, etc. — don't buffer whole bodies).
   - Forward and rewrite the `Set-Cookie` / `Cookie` headers so auth cookies survive
     same-origin under the sub-path.
   - Preserve status codes, content-type, and `Location` redirects (rewrite to keep the
     `/greatreads` prefix).
   - If the Python proxy proves fragile under streaming/concurrency, fall back to **nginx**
     (GreatReads already ships `greatreads_nginx_config.txt`).
2. **Auth bypass** (OQ3): auto-login / disable the login gate for the integrated build so the
   button lands on the GreatReads home, not `/login`.
3. **Home-page entry point** in `web/index.html`: add a "GreatReads" button/menu item →
   `/greatreads/`. Match Ereader's existing button styling.
4. **Back navigation**: ensure browser/WebView back from GreatReads returns to the Ereader
   library (history behaves; the WebView back button already does history nav).
5. Verify the Android WebView path end-to-end (same-origin matters here — that's why we proxy
   rather than link cross-origin to :8092).

### Acceptance criteria
- From the Ereader home screen **and inside the Android app**, tapping "GreatReads" opens the
  full GreatReads UI served from this repo against our data, with working assets and nav.
- Back-navigation returns to the Ereader library.
- Reader, player, highlights, and progress all still work (smoke-tested).

### Risk → mitigation
- *Cross-origin cookie/nav breakage in WebView* → serve **same-origin** under `/greatreads/`.
- *Proxy streaming bugs* → nginx fallback. *Only additive changes to `index.html` + `serve.py`;
  `server.py` and data files untouched.* **Risk to Ereader: very low.**

### Definition of Done
Button ships; GreatReads usable inside the app from this repo's code+data; Ereader intact.

---

## Story 2 — Cross-link the two sides; retire the remote `:8007` dependency
**Size:** L · **User-visible:** yes · **Depends on:** Story 1 · **STATUS: ✅ DONE (2026-06-15)**

> **Done:** `backend/server.py` `GREATREADS_URL` now defaults to `http://127.0.0.1:8092`; the
> transitional dual-write mirror (`GREATREADS_MIRROR_URLS` / `_gr_mirror`) was removed. The old
> prod container `greatreads_app` (:8007) is **stopped** (data dir retained as a cold backup), and
> APScheduler (Calibre/ABS auto-sync + midnight chain recalc) is now **enabled on :8092**
> (`ENABLE_SCHEDULERS=true`) as the sole writer. Daily online DB backups run via
> `greatreads/scripts/backup-db.sh` (cron 02:30). Deep links between reader/player and GreatReads
> remain a follow-up. NOTE: stopping :8007 also took down the public `forge-freedom.com/greatreads`
> route — repoint the host reverse proxy to :8092 if that external URL is still needed.

**As** a user **I want** books to link between the reader/player and GreatReads, and **as** the
maintainer **I want** all GreatReads traffic to hit the local service **so that** the remote
`100.69.184.113:8007` is no longer a dependency.

### Scope / sub-tasks
1. **Identity resolution helper**: given a Calibre id or `abs:<uuid>`, resolve the GreatReads
   `book_id` via `external_imports(source, external_id)`. Build it once; reuse everywhere.
2. **Deep links**:
   - Ereader library/book → "Open in GreatReads" (→ `/greatreads/...book_id`).
   - GreatReads book → "Read" (→ `reader.html?id=<calibre_id>`) / "Listen"
     (→ `player.html?id=abs:<uuid>`) using the reverse mapping.
3. **Repoint the existing finish/sync flows**: change `backend/server.py`'s
   `/api/greatreads/*` (`sync`, `format/<id>`, `finish`, `start-next`) from the remote
   `GREATREADS_URL=:8007` to the local `:8092`. Same request shapes, same chain rules
   (next = row where `id_previous == current.id`). Grep all `GREATREADS_URL` / `:8007` refs.
4. **Enable APScheduler** in the vendored service (OQ6): midnight chain recalc + 15-min
   Calibre/ABS sync, now that the local DB is the one we rely on. Verify no double-sync vs any
   still-running upstream; **decommission the old `:8007` deployment** once parity is proven.
5. Update `GREATREADS_INTEGRATION.md` to point at the local service (or mark it superseded).

### Acceptance criteria
- Finish / start-next / sync / format flows work against the **local** DB; zero traffic to
  `:8007` (verified by logs / netstat).
- Deep links work both directions and resolve via `external_imports`.
- Schedulers run locally without duplicate writes.

### Risk → mitigation
- *Chain corruption from bad next-link logic* → reuse the documented `id_previous` rule, dry-run
  first. *Two syncers racing the DB* → shut down upstream before enabling local scheduler.

### Definition of Done
Remote `:8007` is unused and can be turned off; cross-links live; integration doc updated.

---

## Story 3 — Unify the data layer (migrate Ereader JSON → SQLite)
**Size:** L · **User-visible:** no (behavior identical) · **Depends on:** Story 2

**As** the maintainer **I want** Ereader's progress/highlights/requests stored in the SQLite DB
**so that** there's one source of truth and the JSON jank is gone.

### Scope / sub-tasks
1. **Schema additions** (SQLAlchemy models + migration files, matching GreatReads' migration
   style in `migrations/`):
   - `progress(book_ref, format, anchor, page, total, progress_fraction, font_size, updated)`
   - `highlights(id, book_ref, type[highlight|bookmark], anchor, offset, length, page, total,
     text, note, color, created)`
   - `requests(...)` (optional; could stay JSON or move too).
   - `book_ref` joins to `books.id`; resolve via `external_imports` for Calibre/ABS-keyed rows.
     Decide the canonical key (recommend store GreatReads `book_id` + keep the external id).
2. **Migration scripts** (one-time, idempotent, reversible from backup): read
   `backend/data/{progress,highlights,requests}.json` → upsert into the new tables. Log
   unmatched rows (books not yet in GreatReads) and how they're handled (create stub vs skip).
3. **Dual-write window**: temporarily write both JSON and SQLite from the progress/highlight
   endpoints; compare for drift; then cut reads over to SQLite; then stop writing JSON.
4. **Repoint APIs**: the reader/player progress + highlight endpoints (still in Flask at this
   point) read/write SQLite instead of JSON. (They can talk to SQLite directly, or call the
   FastAPI service — pick one; direct SQLite is simplest pre-Story-4.)
5. Keep the JSON files as a backup snapshot; document rollback.

### Acceptance criteria
- "Currently reading" progress shown in GreatReads comes from the **same row** the reader
  writes — no JSON/SQLite drift.
- Highlights/bookmarks survive a round-trip through SQLite identically to JSON.
- Migration is reversible from the retained JSON backup.

### Risk → mitigation
- *Data loss in migration* → dual-write + retain JSON + idempotent re-runnable scripts.
- *ID mismatch (Calibre vs GR book_id)* → resolve through `external_imports`, log/handle misses.

### Definition of Done
Progress/highlights live in SQLite; reader/player use it; JSON retired to backup.

---

## Story 4 — Collapse to one backend (port Flask → FastAPI, delete Flask)
**Size:** XL · **User-visible:** no · **Depends on:** Story 3

**As** the maintainer **I want** a single FastAPI backend **so that** there's one stack, one
process, one dependency tree (D1/D4).

### Scope / sub-tasks
1. Port Ereader's Flask routes into the FastAPI app as routers, preserving exact paths/shapes
   the frontend expects:
   - Calibre proxy (`/api/books*`, covers, downloads, search), `/api/library`, `/api/series`,
     `/api/saga`, `/api/summaries`, audiobook/ABS routes incl. **HLS proxy** and playback
     sessions, highlights, progress, requests, health/version/build-stamp.
   - The audiobook **HLS streaming proxy** is the trickiest port (chunked/streamed responses) —
     give it its own sub-task and explicit streaming tests.
2. Move the `/api/greatreads/*` finish/sync logic into native FastAPI service calls (no more
   HTTP hop — it's the same process now).
3. Single process model: one FastAPI app serves APIs **and** the GreatReads pages; `web/serve.py`
   either proxies to it or is replaced by serving static reader/player assets via FastAPI
   `StaticFiles`. Keep `Cache-Control: no-store` semantics the WebView relies on.
4. **Delete `backend/server.py` and the :8091 service**; collapse to one venv / `pyproject.toml`.
5. Full regression pass: reader, player (incl. lock-screen media + HLS), highlights, progress,
   library/series/saga, summaries, GreatReads pages.

### Acceptance criteria
- One FastAPI process serves every endpoint the frontend uses; `:8091`/Flask removed.
- Audiobook HLS playback, media-session, and progress all work through the new backend.
- No regressions across reader/player/library/GreatReads.

### Risk → mitigation
- *Behavioral drift on ported routes* → port path-for-path, contract-test against the live
  frontend, keep Flask runnable on a branch until parity proven. **Highest-risk story — do it
  only after Stories 0–3 are solid and the app is otherwise stable.**

### Definition of Done
Single FastAPI backend; Flask deleted; full regression green.

---

## Story 5 — App-shaping: make GreatReads feel like (and work as) an Android app
**Size:** XL (split into sub-stories) · **User-visible:** yes · **Depends on:** Story 1+
(diagnosis can start anytime; see dedicated **§ Android Readiness Diagnosis** below)

**As** a user **I want** GreatReads to look and behave like a native part of the app **so that**
the unified product feels like one Android app, not a website embedded in a reader.

This story is large; it's broken into sub-stories 5a–5g, each independently shippable. The
concrete defects driving them are enumerated in **§ Android Readiness Diagnosis**.

- **5a — Vendor frontend assets locally (offline + reliability).** Bundle Bootstrap, Font
  Awesome, axios, SortableJS into local static files; remove all CDN `<link>/<script>` from
  `base.html`. *Highest priority* — today the app is dead without internet. (Diagnosis A1.)
- **5b — App chrome & navigation.** Replace/augment the Bootstrap top-navbar+hamburger with an
  app-style **bottom tab bar** (or fold GreatReads pages into Ereader's existing nav), drop
  desktop flourishes (MT header clock, `d-none d-lg-*` bits). (Diagnosis A4, A9.)
- **5c — Safe-area & WebView fit.** Add `viewport-fit=cover`, `env(safe-area-inset-*)` padding,
  `theme-color`, and a **web app manifest** so it renders correctly under notches/cutouts the
  way Ereader's WebView (SHORT_EDGES + immersive) expects. (Diagnosis A2, A3.)
- **5d — Touch-first interactions.** Remove hover-only affordances (14 `:hover` rules);
  guarantee ≥44px tap targets; make modals mobile-friendly (sheet-style, scroll-locked safely).
  (Diagnosis A5, A6.)
- **5e — Reflow table-heavy pages.** Convert `books.html`, `library.html`, `settings.html`
  tables to card/list layouts on narrow screens. (Diagnosis A7.)
- **5f — Drag-reorder on touch.** SortableJS already has touch support (good), but validate TBR
  and bookshelf reordering on a real device, tune `delay`/`handle` so drags don't fight scroll.
  (Diagnosis A8.)
- **5g — Theme unification.** Align GreatReads' "bookish" purple/Bootstrap theme with Ereader's
  dark immersive look (shared CSS variables, fonts) so the two halves feel like one app.
- **5h — Android bridge integration.** Where useful, expose GreatReads to the `window.Android`
  bridge (system bars, share, keep-screen-on); consider surfacing GreatReads in the WebView's
  native nav; revisit `OFFLINE_PLAN.md` for cached library browsing.

### Acceptance criteria (per sub-story; overall)
- App functions with **no network to the internet** (CDN removed) — only the local backend.
- GreatReads pages render correctly edge-to-edge on a notched Android device, with app-style
  nav, touch-sized targets, no hover-dependent actions, and reflowed (non-overflowing) layouts.
- TBR/bookshelf reorder works by touch. Visual theme matches the reader/player.

### Definition of Done
GreatReads is indistinguishable from a first-class part of the Android app across the major
pages; offline-safe for its own assets.

---

## Story 6 — Cleanup & consolidation (debt paydown)
**Size:** M · **Depends on:** Story 4

- Remove dead **React Native scaffolding** in `app/` (never compiled; app ships as WebView).
- Reconcile duplicate config / guidelines files across both repos (`.augment*`, `AGENTS.md`,
  `CLAUDE.md`, docker-compose variants).
- Address known **chain edge cases** documented in `CHAIN_SYSTEM_ANALYSIS.md` (historical
  circular `id_previous`, frontend sorting by non-existent fields).
- Fold GreatReads' `scripts/` (merge-duplicates, user setup, migrations) into a single
  maintenance toolkit; delete the now-obsolete remote-sync code paths.
- Single `version.txt` + `build-app.sh` pipeline drives the merged app's APK (don't fork it).

---

## Story 9 — Rename the repo to GreatReads (product identity)
**Size:** S · **Depends on:** Stories 0–4 substantially done · **Non-blocking**

You prefer the name "GreatReads"; this (Ereader) is the stronger base, so we build here and
rename. At rename time: GitHub repo rename (auto-redirects old URLs) · update `git remote` ·
Android **package id / app name / icons** in `simple-app/` · fix hardcoded paths and the
`GREATREADS_SOURCE` / `GREATREADS_CHAIN_REFERENCE.md` symlinks · purge references to
`http://100.69.184.113:8007`. Do it whenever convenient after the backends merge.

---

# § Android Readiness Diagnosis (input to Story 5)

Findings from inspecting GreatReads' `templates/base.html`, `static/css/style.css`,
`static/js/app.js`, `tbr.html`, `bookshelves.html`. Severity: 🔴 blocker, 🟠 important, 🟡 polish.

| ID | Severity | Finding | Evidence | Fix (sub-story) |
|----|----|---------|----------|-----|
| **A1** | 🔴 | **All front-end assets load from CDN** — Bootstrap CSS/JS, Font Awesome, axios, SortableJS via jsdelivr/cloudflare. App is **broken offline / on bad networks**. Ereader by contrast serves everything locally. | `base.html:38,41,178,181,184` | **5a** — vendor assets locally, remove CDN refs |
| **A2** | 🟠 | **No web app manifest, no `theme-color`** — no Android standalone/PWA affordances or status-bar theming (only Apple `apple-mobile-web-app-*` meta present). | `base.html:5,14–16` | **5c** |
| **A3** | 🟠 | **No safe-area handling.** Viewport is `width=device-width, initial-scale=1.0` with **no `viewport-fit=cover`** and no `env(safe-area-inset-*)`. Under Ereader's notch-aware immersive WebView (SHORT_EDGES), content can sit under the cutout/nav. | `base.html:5`; no `safe-area` in `style.css` | **5c** |
| **A4** | 🟠 | **Desktop nav pattern.** Bootstrap `navbar-expand-lg` top bar with hamburger collapse + 8 links — not an app bottom-tab pattern; also collides conceptually with Ereader's own nav once embedded. | `base.html:51–110` | **5b** |
| **A5** | 🟠 | **Hover-dependent affordances** (14 `:hover` rules). Hover doesn't exist on touch — anything revealed only on hover is unreachable. | `style.css` (14 `:hover`) | **5d** |
| **A6** | 🟠 | **Bootstrap modals everywhere** (tbr 9, books 8, bookshelves 8, library 6, index 6, journal 3). Workable but need mobile sheet-style UX + safe scroll-lock; backdrops can be janky in WebView. | grep modal counts | **5d** |
| **A7** | 🟠 | **Table-heavy pages** (`books.html`, `library.html`, `settings.html`). Wide tables overflow / don't reflow on phone widths. | `grep <table>` | **5e** |
| **A8** | 🟢 | **Drag-reorder already uses SortableJS** (TBR ebook/audio/physical lists, bookshelf tabs) — has touch support, so this mostly *works* on touch. Just validate on-device and tune `handle`/`delay` vs scroll. | `tbr.html:1213,2132`; `bookshelves.html:401` | **5f** (low risk) |
| **A9** | 🟡 | **Desktop flourishes**: header clock w/ Mountain-Time label, `d-none d-lg-flex` elements hidden on mobile (dead weight). | `base.html:110+` | **5b** |
| **A10** | 🟡 | **Coarse responsiveness**: only 5 media queries at 768/991px; not truly mobile-first. Tune for phone-first. | `style.css` 5 `@media` | **5d/5e** |
| **A11** | 🟡 | **axios vs fetch**: GreatReads uses axios, Ereader uses native `fetch`. Minor inconsistency; either vendor axios (5a) or migrate to fetch during cleanup. | `base.html:181` | **6** |

**Top takeaway:** the single most important Android-readiness fix is **A1 (kill the CDN
dependency)** — without it the embedded app fails whenever the device is offline or on a poor
connection, which is exactly when a reader app gets used. Everything else is layout/UX polish
on a fundamentally responsive Bootstrap base.

---

## Appendix — environment facts (verified 2026-06-14)
- Ports in use: 8080, 8083 (Calibre), 8084, 8085, 8090 (Ereader web), 8091 (Ereader API),
  8096, 8098, 8099. **:8092 free** → vendored GreatReads service.
- Canonical DB: `GreatReads/data/greatreads.db` (593 KB, active WAL) + `covers/`,
  `covers_thumb/`. `data-dev/` exists for dev.
- `web/serve.py` is 95 lines (`ThreadingHTTPServer` + `SimpleHTTPRequestHandler`, no proxy yet
  — Story 1 adds the proxy).
- GreatReads source commit when vendored: `dbafbc1 v2.1.7`.
- Ereader version: `version.txt` (1.2.61); APK built by `build-app.sh` → `web/ereader.apk`.
