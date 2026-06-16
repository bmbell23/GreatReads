---
type: always
description: Ereader agent guidelines — extends shared server guidelines
---

# AI Agent Guidelines for Ereader

**Shared server guidelines** (iptables fix, container inventory, Tailscale IP, forbidden ops):
→ `/home/brandon/projects/.augment-guidelines`

---

## 🚨 Session Start Checklist (Do This First, Every Session)

**CRITICAL: Run this checklist at the start of EVERY conversation turn, not just the first message. Services can crash mid-session.**

1. **Health-check both services:**
   - Backend: `curl -s --max-time 3 http://localhost:8091/api/health`
   - Static server: `ss -ltnp | grep :8090`
2. **If either is down, restart it immediately** (see "Restarting Services" below) — do not ask the user first, just fix it and report what you did.
3. **Verify backend API responses aren't throwing errors:**
   - `curl -s http://localhost:8091/api/library?limit=3` should return JSON, not HTML error pages
   - If you see HTML or OSError, restart the backend

**If the user reports nothing loading / black screen / broken app: See `docs/RECOVERY.md`.**

---

## �🚨 Critical Rules (Always Apply)

- **NEVER**: `sudo reboot`, `shutdown`, `docker-compose down`, `DROP DATABASE`, `pg_resetwal`
- **NEVER** pipe `curl … | bash` from third-party sources without explicit user confirmation
- **NEVER** commit, push, install dependencies, or build/flash the APK without explicit permission
- **THIS FILE** (`.augment/rules/ereader-guidelines.md`) **is the source of truth** for project layout/ports/APIs/invariants. Keep reference docs in `docs/` (e.g. `docs/RECOVERY.md`, `docs/OFFLINE_PLAN.md`, `docs/merge-plan.md`); don't scatter `*.md` back into the repo root.
- **Tailscale IP**: `100.69.184.113`
- Not a Docker project — runs as a bare Flask process + static web files

## Project Details

- **Directory**: `/home/brandon/projects/Ereader/`
- **User-facing product name**: **GreatReads** (HTML titles, share-sheet strings, force-stop target). The repo / package name is `ereader` / `com.ereader.simple` — they refer to the same app.
- **Backend**: Flask, `backend/server.py`, port **8091**, proxies a Calibre Content Server
  - Calibre upstream: `$CALIBRE_URL` (default `http://localhost:8083`), library `$CALIBRE_LIBRARY`
  - Run via `backend/run.sh` (creates venv, installs requirements, starts on 0.0.0.0:8091)
  - PID file: `server.pid`, logs: `backend/server.log`
  - **Audiobookshelf (ABS)**: optional second source for audiobooks. `$ABS_URL` (default `http://localhost:13378`, the local `audiobookshelf` container → :80), `$ABS_TOKEN` (per-user API token), `$ABS_LIBRARY_ID` (optional; first `book` library if blank). `$ABS_PUBLIC_URL` (optional; the host the **phone** uses to fetch HLS/media directly — defaults to `$ABS_URL` with a `localhost`/`127.0.0.1` host swapped for the Tailscale IP `100.69.184.113`, since the WebView can't reach the server's localhost). Credentials live in `backend/abs.env` (gitignored; template `backend/abs.env.example`), sourced by `run.sh`. `ABS_ENABLED = bool(ABS_URL and ABS_TOKEN)` — when off, every ABS path no-ops and the library degrades to Calibre-only (never 500s). Read-only.
- **Web reader**: static files in `web/` served on port **8090** (separate process)
  - `web/index.html` — library browser (~1278 lines). Consumes `/api/library` (Calibre+ABS merged); cards show 📖/🎧 media-type badges (audio-only → 🎧, dual → 📖🎧, ebook-only → none). Tap routing: ebook-only → reader, audio-only → player, dual → Read/Listen action sheet. **Sort modes:** Author (actively in-progress books — `0 < progress < 1` — newest-updated first, then rest by author last name → series → series_index → published), Title (pure A→Z), Series (grouped grid), Saga (grouped grid). `applySort()` implements the client-side portion; backend pre-sorts `/api/library` by author but `ensureProgressBooksLoaded()` prepends missing in-progress books, so client must re-sort. Books with `progress === 0` (opened but not read) or `progress >= 1` (finished) sort with the rest, not as in-progress.
  - `web/reader.html` — PDF + EPUB reader (~4450 lines, consumed in-app via WebView)
  - `web/player.html` + `web/player.js` — audiobook player (consumed in-app via WebView). Opened as `player.html?absId=&title=&author=` (dual-format works also pass `&bookId=&format=&hasEbook=1` so the player can open the ebook). Loads hls.js from CDN; starts an ABS session via the backend play route, plays HLS (transcode) or direct single-file. UI uses the brand (bi-pride) gradient `#4067EF→#8640C0→#C940B0`; large cover; PNG play/pause button (`web/play.png` / `web/pause.png`, inverted to white over the gradient). Two seekable progress bars (book + chapter), each showing elapsed / remaining + percent — **remaining is wall-clock time at the current speed** (`remaining_content / rate`). Speed is a slider 1.0×–3.0× in 0.1 steps, shown in a pill and **persisted in `localStorage['ereader.audio.speed']`** so it propagates across books (re-applied on each media load). Resume position is persisted to OUR backend (`PUT /api/progress/abs:<absId>`) every ~15s sync + on close, and read back on open — independent of ABS's own session currentTime. It is ALSO mirrored synchronously to `localStorage['ereader.state.abs:<absId>']` (`{position,duration,progress,mediaType:'audiobook',ts}`, same shape/prefix the EPUB reader uses for `ereader.state.<bookId>`) — written on the throttled (~2s) UI tick and on close. The library reads this snapshot as a freshness-winning fallback so the audio progress bar updates instantly on return even before the keepalive backend PUT commits (avoids the back-button race where `index.html` re-fetches `/api/progress` before the player's final save lands). When `hasEbook`, a 🔍 button opens `reader.html` in a same-origin iframe overlay and calls its `openSearch()`; audio keeps playing while reading/searching; the player's ‹ back button closes the session (stops audio). Also: ±30s, prev/next chapter, chapter drawer, Media Session lock-screen controls.
  - `web/about.html` + `web/about.js` — in-app About page ("ℹ️ About" hamburger entry). Documents the reading-speed algorithm and shows live `/api/version` + `/api/build-stamp`.
  - `web/serve.py` — preferred static server (no-cache headers, correct `.apk` MIME, permissive CORS). Use this over `python3 -m http.server` so the WebView never serves stale HTML.
- **Android app**: `simple-app/` — minimal native WebView wrapper pointing at `http://100.69.184.113:8090`
  - Single Activity: `simple-app/app/src/main/java/com/ereader/simple/MainActivity.java`
  - `minSdk 24`, `targetSdk 34`, `compileSdk 34`, JDK 17
  - Manifest declares `android:configChanges=...|screenSize|...|orientation|...` so foldable posture changes don't recreate the Activity — they fire `onConfigurationChanged` instead.
  - Suppresses the Chromium text-selection floating toolbar via `wrapEmptyMenu()` while keeping selection itself working (see in-file comment for full history of why).
  - **Background audiobook playback**: `PlaybackService.java` is a foreground service (`foregroundServiceType=mediaPlayback`) that keeps the WebView `<audio>` playing when the screen locks and owns the `MediaSessionCompat` that hardware/headphone/lock-screen buttons drive. Audio itself stays in the WebView — the service only (a) holds a `PARTIAL_WAKE_LOCK` while playing (a foreground service alone still gets Dozed after a few minutes, which throttles the JS thread and stalls hls.js segment fetches — audio dies while `<audio>` still reads as "playing"), (b) holds the session + posts an ongoing `MediaStyle` notification, and (c) forwards button callbacks to JS (see "JS ↔ Java Bridge"). State/stop pushes from JS go in-process via the static `PlaybackService.applyState`/`stopFromBridge` (not repeated `startForegroundService`, which Android 12+ blocks from the background once locked). Manifest adds `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MEDIA_PLAYBACK`, `WAKE_LOCK`, `POST_NOTIFICATIONS` (runtime-requested on Android 13+ in `onCreate`), declares the service + an `androidx.media.session.MediaButtonReceiver`, and depends on `androidx.media:media`.
  - Build output: `web/ereader.apk` (debug-signed via `~/.android/debug.keystore`, served from the static server as an in-place upgrade — no uninstall needed).
  - **Do not rebuild or flash the APK without permission.**
- **React Native app**: `app/` — older RN attempt, not currently the primary client. EPUB reading is `coming soon` placeholder. Don't touch unless asked.

## Versioning Workflow

- `version.txt` at repo root is the **single source of truth** for the app version (semver).
- Bumped by the user's `gvc` shell function (`~/dotfiles/bashrc/conf.d/20-functions.sh`) which auto-increments the patch, commits, tags, and pushes.
- Read live by:
  - `backend/server.py` → `/api/version` (no restart needed after a bump)
  - `simple-app/app/build.gradle` → `versionName` = file contents, `versionCode` = `major*10000 + minor*100 + patch`
- **Never edit `version.txt` directly** unless the user explicitly asks. Let `gvc` handle it.

## Backend API Surface (`backend/server.py`, port 8091)

All endpoints are JSON unless noted. CORS is permissive.

- **Books (Calibre proxy)**
  - `GET  /api/books?limit&offset&query` — list books (sorted by author last name, then series, then pubdate)
  - `GET  /api/books/<id>` — single book metadata; includes `wordCount` from the Calibre `#word_count` custom column (null when unset) and `asin` from the optional `#asin` custom column (`''` when unset; used for ABS matching)
  - `GET  /api/books/<id>/cover?type=cover|thumb` — proxied image (30-day cache, SVG placeholder fallback). `type=thumb` asks Calibre for a grid-sized thumbnail via `?sz=400x600` (its default thumb is a useless 60x80) — ~7x smaller than the full cover; Calibre caches the scaled result on its own disk. The web grid + series cards use the thumbnail and lazy-load covers (IntersectionObserver, `web/index.html` → `_lazyLoadCovers`), caching blobs in IndexedDB keyed by cover URL (variant-aware: thumb vs full stored separately). **Cache-bust:** the `thumbnail`/`cover` URLs that `get_book_metadata` emits carry a `?v=<token>` (digits of Calibre's `last_modified`); the endpoint ignores `v`, but because the frontend IndexedDB cache + the 30-day HTTP cache are keyed by full URL, replacing a cover in Calibre bumps `last_modified` → new `v` → caches self-invalidate. Without it a stale cover is pinned indefinitely.
  - `GET  /api/books/<id>/download?format=epub|pdf|...` — streams the file from Calibre
  - `GET  /api/search?q&limit&offset` — same as `/api/books` with a query
- **Audiobooks / unified library (Audiobookshelf)** — additive; `/api/books` + `/api/search` are unchanged. All degrade to Calibre-only / empty when ABS is off.
  - `GET  /api/library?limit&offset&query&q` — merged Calibre + ABS list. Ebook items keep their **Calibre numeric id** (so progress/highlight/cache keys still resolve); audio-only items use **`abs:{absId}`**. Each item gets `mediaTypes` (`['ebook']` / `['ebook','audiobook']` / `['audiobook']`); matched items also carry `absId`, `audioCover`, `narrators`, `audiobook{duration,chapters}`. Match order per work: manual `links.json` → ISBN → ASIN → normalized title+first-author, with two fallbacks: subtitle-stripped, then edition-marker-stripped (drops `(Unabridged)`/`(Dramatized…)`/`(Part N of M)`/`[…]`/leading `03 - ` track numbers; first author is comma-split so ABS co-author strings reduce to the lead). All title tiers are author-gated, so the strips stay false-positive-free. ABS display titles are pre-cleaned in `normalize_abs_item` (edition/format markers like `(Unabridged)` stripped so they never reach the UI); the original is kept in the private `_rawTitle` field so `_group_editions` can still parse parenthesised `(Part N of M)` markers for multi-part stitching. **Matching is precomputed once over the FULL library** (`_get_library_cache()`, 120 s TTL) — not per page — so a dual-format work is detected no matter which Calibre page its ebook lands on, and only genuinely unmatched ABS items become audio-only. Returns `absEnabled`. Two modes: **browse** (no `query`) appends the audio-only list only on `offset==0`; **search** (`query` = Calibre ebook query, `q` = raw term) enriches the Calibre hits with ABS data AND appends audio-only items whose title+author contains every whitespace token of `q` (so audiobooks surface in search too).
  - `GET  /api/audiobooks` — debug: raw normalized ABS list + `absEnabled` flag
  - `GET  /api/audiobooks/<absId>/cover?type=cover|thumb` — proxies ABS `/api/items/{id}/cover` (30-day cache, 🎧 SVG placeholder fallback). `type=thumb` adds `?width=400` (ABS resizes server-side; ignored if the stored cover is already small — ABS covers are tiny webp, so this is a near-no-op safety hint).
  - `POST /api/audiobooks/<absId>/play` — starts an ABS playback session (`POST /api/items/{id}/play`) and returns it with each `audioTracks[].contentUrl` rewritten. **HLS** tracks (`/hls/<sid>/output.m3u8`) point at the backend HLS proxy below (no token); **direct** single-file tracks point at an absolute `?token=` URL on `$ABS_PUBLIC_URL` (a plain `<audio>` element plays those cross-origin without CORS). Read `playMethod` (0=DirectPlay,1=DirectStream,2=Transcode), `currentTime` (resume point), `duration`, `chapters`, `audioTracks`. **Multi-file guard:** a DirectPlay split across >1 file is closed and re-requested with `supportedMimeTypes:[]` to force one stitched HLS manifest. 503 when ABS off.
  - `GET  /api/audiobooks/hls/<path:subpath>` — proxies the ABS HLS manifest + `.ts` segments (token re-attached server-side, so it never reaches the phone). Needed because ABS sends no `Access-Control-Allow-Origin` on `/hls`, so hls.js's XHR from the `:8090` WebView would be CORS-blocked; routing through `:8091` (permissive CORS) fixes it. Manifest segment names are relative, so they resolve back to this route — no body rewriting. ABS transcodes segments on demand, so a fresh `.ts` 404s until ffmpeg produces it; the proxy retries (~6s) then passes the upstream status through for hls.js to retry too. Does stream audio bytes (one LAN hop, unavoidable for CORS).
  - `POST /api/audiobooks/sessions/<sid>/sync` — forwards to ABS `POST /api/session/<sid>/sync`. Body `{currentTime, timeListened (delta seconds since the previous sync, NOT cumulative), duration}`. Call ~every 15s while playing.
  - `POST /api/audiobooks/sessions/<sid>/close` — forwards to ABS `POST /api/session/<sid>/close`; optional body forwarded as a final sync. Always returns `{ok:true}` (best-effort; client calls it via `sendBeacon` on navigate-away).
- **Highlights & bookmarks** (see "Highlight Model" below)
  - `GET    /api/highlights?bookId&type&q` — filtered list, newest first
  - `POST   /api/highlights` — create (server fills `id` + `created`)
  - `PUT    /api/highlights/<id>` — partial update; only `note`, `color`, `text` are mutable
  - `DELETE /api/highlights/<id>`
- **Per-book reading progress** (cross-device "where am I")
  - `GET    /api/progress` — all books, newest-updated first (powers "Continue reading")
  - `GET    /api/progress/<bookId>`
  - `PUT    /api/progress/<bookId>` — upsert; last-writer-wins on `updated` timestamp. Body may include `recentPages` (rolling buffer of `{ms, words}` page-turn samples, see "Highlight & Bookmark Model" → progress record)
  - `DELETE /api/progress/<bookId>`
- **Series**
  - `GET  /api/series` — groups the full merged library (Calibre + ABS) into series. Reuses the full-library cache so grouping spans all Calibre pages and audio-only items. Returns `{series:[{name, count, mediaTypes, cover, books:[…]}, …], absEnabled}`. Books with no series are omitted; books within each group are sorted by `_series_sort_key` (unnumbered `series_index: null` first — ahead of 0/negatives — then numeric ascending, then title). Series cards are sorted alphabetically. Used by the frontend Series sort pill. The frontend series-detail overlay renders a `#N` badge (bottom-left of each card, lifted above any progress bars) via `bookCardHTML(book, {showSeriesNum:true})`; unnumbered books show no badge. **ABS bakes the sequence into the series name** (`"Dresden Files #10.4"`, `"A Song of Ice and Fire #3"`) with `series_index = 0`, which would split every audiobook into its own one-book group — `normalize_abs_item` calls `_split_abs_series` to peel the trailing `#N`/`#N.N` off into a clean base name + numeric `series_index`, so audio-only items group under the same series as their Calibre ebook counterparts (read-only against ABS; the fix lives entirely in our normalization).
- **Saga** (Calibre `#universe` custom column, label "Universe", e.g. "The Cosmere")
  - `GET  /api/saga` — groups the library into sagas by the Calibre `#universe` custom column (read in `get_book_metadata`, surfaced as the `universe` field; `''` when unset, also part of `_SERIES_BOOK_FIELDS`). Same payload shape as `/api/series` but keyed `{sagas:[{name, count, mediaTypes, cover, books:[…]}, …], absEnabled}`. Books with no universe are omitted. **Calibre-only**: ABS has no universe field, so audio-only items (no `universe`) are naturally excluded; dual-format works still appear via their Calibre side. Books within a saga are sorted by `_saga_sort_key` (by series name, then `series_index` with unnumbered first, then title) so a saga reads as its constituent series in order; seriesless books sort last. Sagas are sorted alphabetically. Used by the frontend Saga sort pill; the saga-detail overlay reuses the series overlay and renders plain `bookCardHTML` cards (no `#N` badge — a saga spans multiple series, so a bare index would be ambiguous).
- **Chapter summaries** (overlay shown at chapter-end / from Reading Options)
  - `GET  /api/summaries/<bookId>` — resolve a book to a pre-written chapter-summary "set" and return that book's ordered chapter summaries: `{available, setId, setTitle, source, bookTitle, matchedBy, chapters:[{title, html}]}`. `{available:false}` (never an error) when nothing matches, so the reader quietly hides the 📖 button. **Matching:** manual override (`backend/data/summary_links.json`, `{bookId:{set,book}}`) → else normalized book title (lowercase, drop leading article, non-alnum→space) against the set's `books[].title`. Audio-only `abs:` ids skip Calibre and never match.
  - **Summary sets** are committed reference assets in `backend/summaries/<id>.json` (a *tracked* dir, NOT `backend/data/`), shaped `{id, title, source, books:[{title, chapters:[{title, html}]}]}`. Loaded + indexed once, cache-keyed by (file, mtime) so edits/new sets need no restart. Built by `backend/build_summaries.py SOURCE.mht OUT.json --id <id> --title "<Title>"`. **Source quirk:** the *Malazan Book of the Fallen Compendium* (highnessatharva.github.io/Malazan-Compendium) ships several formats in `books_staging/`; the `.epub` lost its smart punctuation to U+FFFD replacement chars, so parse the **`.mht`** (clean UTF-8, same `<h1>`=book / `<h2>`=chapter structure). Current set: `malazan.json` (10 books, Gardens of the Moon → The Crippled God).
  - **Reader UI** (`web/reader.html`): `loadBookSummaries()` fetches on EPUB load; 📖 in Reading Options + a floating end-of-chapter nudge open `#summary-overlay`.
- **Fetch proxy** (powers the reader's clean in-app lookup view)
  - `GET  /api/fetch?url=<encoded>` — server-side GET of a public **http/https** URL, body passed through with its content-type, capped at 4 MB. **Not an open proxy:** scheme-restricted and loopback/private/link-local hosts are refused (`_fetch_host_blocked`, basic SSRF guard) since the only legit targets are public reference sites. Used by the reader to fetch the MediaWiki action API + dictionaryapi.dev without CORS.
  - **Reader lookups** (`web/reader.html`): highlighting text and tapping 🌐/📖 no longer navigates the WebView to the live (ad-laden, jank-scrolling) page. `openCleanReader(kind,text)` renders results in `#reader-overlay` — OUR own scroll container. **wiki** → MediaWiki action API (`_wikiApiBase()`: `/w/api.php` for wikipedia/wikimedia/wiktionary, else `/api.php`) search→parse, then `cleanWikiHTML()` strips nav/editsection/references/navboxes via DOMParser, neutralizes links, absolutizes image URLs (keeps the Fandom/portable infobox). **dict** → dictionaryapi.dev JSON → simple definitions. Any failure falls back to the old `window.open(configuredURL)`. The wiki/dict URL *patterns* in the Lookup-URL settings still define which site (origin) is used. The current reader chapter is matched to a summary chapter by normalized title (`_summaryNorm`, mirrors the backend; "CHAPTER TWENTY-ONE"≡"Chapter Twenty-One") — **no ordinal fallback**, so a summary never mis-fires onto the wrong/unmatched chapter (BOOK dividers, front matter, Glossary simply show nothing). **Spoiler-safe:** `buildReachedSummaryChapters()` only ever exposes chapters whose `chapterStarts` page ≤ `currentEpubPage`; "Story so far" concatenates those same reached chapters.
- **GreatReads tracker integration** (see "GreatReads Integration" section below for the full upstream API contract)
  - `POST /api/greatreads/sync` — **deprecated no-op** (kept so the reader's fire-and-forget call doesn't 404). Progress is now written straight into the GreatReads SQLite DB on every `PUT /api/progress` via `_gr_set_current_percent` (Story 3); the old title-matching sync job is retired.
  - `GET  /api/greatreads/format/<bookId>` — auto-detect which in-progress reading GreatReads has for this book. Returns `{media: "Physical"|"Ebook"|"Audio"|null, readingId: int|null, status: str|null}`. Pulls `?status=in_progress&limit=1000` and matches by normalized title locally (upstream has no title filter — see below). Returns nulls when no in-progress reading matches; the UI then falls back to inferring media from `mediaTypes`.
  - `POST /api/greatreads/finish` — mark a book as finished in GreatReads. Body: `{bookId, title, author, media, finishDate: "YYYY-MM-DD", rating, ratings: {horror, spice, world_building, writing, characters, readability, enjoyment} (all 0-10 from our slider), readingId: int|null}`. Returns `{success, readingId, message, nextBook: {readingId, alreadyStarted, title, media, id, author, cover, mediaTypes}|null}`. Flow: (1) resolve the reading — use `readingId` if supplied, else search `?status=in_progress`; 404 if no in-progress reading matches (user must start it in GreatReads first). (2) `PUT /api/readings/{id}/` with `date_finished_actual=finishDate` + the seven `rating_*` ints (**halve our 0-10 slider value and round to nearest int 0-5** — GR's native UI is 5 emoji items / `parseInt`; raw 0-10 renders inconsistently because GR's read-side halves anything >5 for legacy compat, so values ≤5 like horror=2 stay 2 while writing=7 renders 4. Sending halved-and-rounded matches what the user sees in GR.). **Never send `rating_overall`** — it's a computed property server-side. (3) Pull `GET /api/readings/tbr`, pick the first item whose `media == finished_media` (excluding the one just finished) and return it as `nextBook`. **Does NOT start the next reading** — that's the caller's job via `/start-next` below, so a Cancel actually cancels. (4) Clear our local progress record(s).
  - `POST /api/greatreads/start-next` — surface a not-yet-started TBR reading as in-progress in GreatReads. Body: `{readingId: int (required), startDate: "YYYY-MM-DD" (optional; defaults to today)}`. Forwards to upstream `POST /api/readings/{id}/start?start_date=…`. Returns `{success, readingId, startDate}` on 200 or 502 on upstream error. The frontend calls this only after the user confirms the "Start reading X next?" prompt from `/finish`.
- **Meta**
  - `GET /api/health` — checks Calibre reachability, returns `version`
  - `GET /api/version` — reads `version.txt` live
  - `GET /api/build-stamp` — `YYMMDD-HH:MM` from the newest of `web/{index,reader}.html`. Drives the status-bar build pill so the user can tell at a glance whether the web code is fresh. Distinct from `/api/version` (which tracks the APK semver).

## GreatReads Integration (upstream API contract)

GreatReads runs as the **vendored, in-repo** FastAPI app at `GREATREADS_URL` (default `http://127.0.0.1:8092`, the `greatreads_ereader` container; source lives in-repo at `greatreads/`). The old remote prod `:8007` is retired (Story 2). No auth — server auto-authenticates as the `brandon` user when no cookie is present; CORS is `*`. Always read-only from our side; **the only write surface we ever touch is `PUT /api/readings/{id}/` (ratings + finish date) and `POST /api/readings/{id}/start` (advance the next book)** — we never POST/DELETE readings or write to books.

**Authoritative read endpoints (use these — do not paginate `/api/readings/` looking for things):**
- `GET /api/readings/tbr` — every unfinished reading **already sorted in reading order** (in-progress first, then not-started by `date_est_start`). This is the canonical "what's next" source per format. Each item carries the joined `book` object. Use it for finding the next book of a given `media` after we mark something finished.
- `GET /api/readings/?status=in_progress&limit=1000` — in-progress only (the common case for resolving a book we're about to finish). `limit` default is 100; bump for safety.
- `GET /api/readings/{id}/` — single reading by id. Use this once we have a `readingId`.
- `GET /api/books/?search=<text>` — book title fuzzy search. **Note:** `/api/books/` ignores any `title=` param; the real filter is `search=` (uses `ilike("%text%")`). Same trap on `/api/readings/`: it accepts only `skip`, `limit`, `status`, `media` — `?book_title=` is silently ignored and returns the first 100 readings, which is the bug that made "mark as finished" pick random books.

**Reading fields we care about** (from `greatreads/src/greatreads/models/reading.py`):
- `id`, `book_id`, `media` (`"Ebook"` | `"Audio"` | `"Physical"` — `"Kindle"` was migrated to `"Ebook"`; no other values)
- `status` (`"in_progress"` | `"not_started"` | `"finished"` | `"paused"` — derived from dates)
- `is_started`, `is_finished`, `is_paused`
- `date_started`, `date_finished_actual`, `date_est_start`
- `id_previous` — linked-list chain pointer per media. **Mostly `null` in the live DB**, so do NOT rely on `id_previous == current_reading_id` to find the next book; that's why our old chain-walk silently did nothing. Use `/api/readings/tbr` instead.
- `rating_horror`, `rating_spice`, `rating_world_building`, `rating_writing`, `rating_characters`, `rating_readability`, `rating_enjoyment` — `Float` columns, but **GR's native UI is a 0-5 integer scale** (5 emoji items, `parseInt` throughout — see `greatreads/src/greatreads/static/js/app.js`). Send `int(round(slider/2))` clamped to 0-5. GR's read-side has legacy 0-10 backward compat: if it sees a stored value `>5` it divides by 2 and rounds, but values `≤5` are shown raw — so sending raw 0-10 produces inconsistent display (e.g. writing=7 → 4 stars, horror=2 → 2 stars; the "horror wasn't halved" symptom).
- `rating_overall` — **computed `@property` server-side** (average of writing/characters/world_building/readability/enjoyment, on whatever scale those were stored). Read-only. Never send it on PUT.
- `current_percent` — 0-100 float. We now write it **directly into the GreatReads SQLite DB** (`read.current_percent` + `current_percent_manual_override` + `date_progress_set`) via `_gr_set_current_percent` on every `PUT /api/progress`, resolving the reading precisely through `external_imports` (Story 3). The old HTTP `PUT /api/readings/{id}/progress` push is no longer used.

**Write paths we use:**
- `PUT /api/readings/{id}/` — partial update via Pydantic `ReadingUpdate`. To mark finished, send `{"date_finished_actual": "YYYY-MM-DD", "rating_*": <0-5 ints>}`. The server runs `ChainCalculator.recalculate_all_chains()` afterward.
- `PUT /api/readings/{id}/progress?current_percent=<float>` — upstream progress-only endpoint. **No longer used by us** — current_percent is written directly to the DB (see above).
- `POST /api/readings/{id}/start?start_date=YYYY-MM-DD` — start a not-started reading (sets `date_started`, recalcs chains). Used to surface the next TBR book as "in progress" after we mark its predecessor finished, because `id_previous` is mostly null so GreatReads' own `finish_reading_and_start_next` no-ops.
- We deliberately **do not** call `POST /api/readings/{id}/finish` (it hard-codes `date.today()` and rejects if `date_finished_actual` is set — useless when the user picks a custom finish date).

**Anti-patterns (these will silently fail or corrupt data — search the codebase for these and remove on sight):**
- `params={'book_title': title}` on `/api/readings/` — param doesn't exist, returns first 100 readings.
- `params={'title': title}` on `/api/books/` — same trap; use `params={'search': title}`.
- Sending raw 0-10 ratings — GR's UI is 0-5 ints; only values >5 get auto-halved on read, so values ≤5 display unscaled. Always `int(round(slider/2))`.
- Sending `rating_overall` on PUT — ignored (read-only property).
- Walking `id_previous` to find the next book — chain links are mostly null. Use `/api/readings/tbr` filtered by `media` instead.
- Using `PATCH` on a reading — the route is `PUT`.

**Reference files (vendored, in-repo):** source at `greatreads/src/greatreads/routes/readings.py` and `.../models/reading.py`; chain math at `greatreads/src/greatreads/services/chain_calculator.py`; chain internals doc at `greatreads/CHAIN_SYSTEM_ANALYSIS.md`. This section is the canonical integration contract.

## Persisted Backend State

- Stored as plain JSON in `backend/data/` (override with `$EREADER_DATA_DIR`):
  - `highlights.json` — list of highlight/bookmark items (lock: `_highlights_lock`)
  - `progress.json` — `{ "<bookId>": {progress record}, ... }` (lock: `_progress_lock`)
  - `links.json` — optional manual audiobook↔ebook overrides for matches auto-matching misses (lock: `_links_lock`). Shape: `{ "<calibre_id>": "<abs_id>", ... }`. Read-only in Phase 1; absent file means no overrides.
  - `series_overrides.json` — optional per-book series overrides for cases Calibre/ABS can't express and we can't write back to (both are read-only sources). Keyed by bookId (`"<calibre_id>"` or `"abs:<id>"`), mtime-cached so a hand-edit is picked up without a restart (lock: `_series_overrides_lock`). Value shapes: `{"series_index": null}` (or shorthand `null`) marks the book **in the series but unnumbered** → it sorts ahead of every numbered book (incl. 0/negatives) and shows no `#N` badge; `{"series": "X", "series_index": 2}` forces both; a bare number forces just the index. Applied in `_apply_series_override`, called from `get_book_metadata` + `normalize_abs_item`. Absent file = no overrides. (Currently: Calibre id 681, *The World of Ice & Fire*, marked unnumbered.)
  - `universe_overrides.json` — optional per-book saga (universe) overrides. Same key shape as `series_overrides` (Calibre id or `"abs:<id>"`). Value is a saga name string (e.g. `"Maasverse"`). mtime-cached (lock: `_universe_overrides_lock`). Applied in `_apply_series_override` after the series override pass. Absent file = no overrides. Use this when the Calibre `#universe` column isn't set but you want a book grouped into a saga — keeps Calibre read-only. (Currently: all Throne of Glass, A Court of Thorns and Roses, and Crescent City books mapped to "Maasverse".)
- `backend/data/` is `.gitignore`d — these are runtime state, not source. Don't re-add them to the repo.
- ABS credentials live in `backend/abs.env` (gitignored; template `backend/abs.env.example`) — never commit the token.
- Atomic writes via `tmp + os.replace`. Trivial to back up; trivial to grep.
- **Read-only access to the Calibre library is the rule** — never write back to Calibre from this project.

## JS ↔ Java Bridge (`window.Android` in the WebView)

Implemented by `MainActivity.JsBridge`. Available **only** inside the APK WebView, never in a desktop browser — feature-detect with `typeof Android !== 'undefined'`.

- `Android.showSystemBars()` — un-hides status + nav bars (reader menu / settings open). Sets `systemBarsRequested = true` so `onWindowFocusChanged` won't snap them back.
- `Android.hideSystemBars()` — re-enters sticky-immersive fullscreen.
- `Android.keepScreenOn(boolean)` — toggles `FLAG_KEEP_SCREEN_ON` on the window (the Web Wake Lock API silently no-ops in this WebView, so this is the reliable path).
- `Android.shareImage(base64Png, chooserTitle)` — writes a PNG to `cacheDir/share/`, wraps it in a FileProvider `content://` URI, and fires `ACTION_SEND` (used for the "share quote" canvas export, because the Web Share API requires a secure context and we serve plain HTTP over Tailscale).
- `Android.mediaStart(title, artist, coverUrl)` — starts/refreshes the foreground `PlaybackService` with this book's metadata (cover fetched off-thread for the lock screen).
- `Android.mediaState(playing, position, duration, rate)` — pushes play/pause + book-global position/duration (**seconds**) + playback rate to the service so the notification/lock-screen scrubber stays fresh. `player.js` calls this forced on play/pause and throttled (~2s) on `timeupdate`.
- `Android.mediaStop()` — tears down the service + notification (player closed / navigated away).
- **Reverse direction**: the service's `MediaSession` callbacks (hardware/headphone/lock-screen + notification buttons) are routed back via `MainActivity.dispatchMedia` → `webView.evaluateJavascript("window.__mediaControl('<action>')")`. `player.js` defines `window.__mediaControl(action)` handling `play` / `pause` / `next` / `prev` / `forward` / `backward` / `seek:<ms>`, driving the `<audio>` element.

## Primary Test Device

- **Google Pixel 10 Pro Fold** — foldable, two postures:
  - Folded (cover screen): ~1080 px CSS width, portrait
  - Unfolded (inner display): ~2076 px CSS width, near-square
- The web reader MUST recompute pagination on `resize` / `orientationchange` / `visualViewport` resize so unfolding doesn't break layout.
- Use `window.visualViewport` when available — more accurate than `window.innerWidth` on foldables and when browser chrome/keyboard is present.

## EPUB Reader (`web/reader.html`) — Layout Invariants

- Pagination = CSS multi-column + `transform: translateX(-page * pageWidth)`.
- **THE ONLY INVARIANT THAT MATTERS:**
  `columnWidth + columnGap == pageWidth / columnCount`
  If this holds, `translateX(-pageWidth)` advances by exactly `columnCount` whole columns and each visible band lands at the identical on-screen position. If it does NOT hold, every page turn drifts by `pageWidth - columnCount*(columnWidth + columnGap)` px, exposing more of the next column on the right.
- Concrete values that satisfy the invariant:
  - `columnGap = 2 * SIDE_PADDING` (gutter on each side of every column)
  - `columnWidth = floor(pageWidth / columnCount) - columnGap`
  - `paddingLeft = paddingRight = SIDE_PADDING` on `#epub-content` (gives the FIRST page its left/right gutters; subsequent pages get them from the column-gap straddling each side)
- DO NOT make `columnGap` conditional on dual-page mode. Same value in both modes.
- All column geometry is computed in `calculatePages()` — do not also set `column-width`, `column-gap`, or horizontal padding in CSS.
- Always call `calculatePages(true)` (preserve progress) on font-size change, dual-page toggle, and viewport resize.

## Highlight & Bookmark Model (`web/reader.html` ↔ backend)

Items are anchored to the *source DOM*, not the paginated layout, so they survive font-size changes, dual-page toggles, and foldable unfolds. Common fields:

- `id` — server-assigned UUID (do not set client-side)
- `type` — `"highlight"`, `"bookmark"`, or `"auto-bookmark"`
- `bookId`, `bookTitle`, `bookAuthor`
- `anchor` — integer index of the source block (`data-anchor="N"` in reader DOM)
- `offset`, `length` — character span inside that single anchor's `textContent`
- `endAnchor`, `endOffset` — populated **only** when the selection crosses anchors (then `length` is null). Single-anchor highlights leave both null for back-compat.
- `page`, `total` — paginated position **at time of creation** (purely informational; never used to re-locate)
- `text`, `note`, `color`, `created` (epoch ms)

The reader re-renders highlights every time it paginates; never trust `page` to find a highlight, always walk the DOM via `anchor` + `offset`.

**Selection → highlight vs lookup:** a settled selection is committed by `scheduleSelectionSettleSave()` (debounce `SETTLE_MS`, now 450 ms — kept short so the action popup feels instant; the highlight-adjust drag still works via the overlap-replace path). Selections of **fewer than `HL_MIN_WORDS` (4) words** are treated as a **lookup**, NOT a highlight: `showLookupPopup()` shows the action popup (copy/📖/🌐, no share/delete) anchored to the live selection and nothing is saved — so quick name/term searches don't litter the highlight list. ≥4 words save as a highlight as before. The popup is shown synchronously on settle (decoupled from the save round-trip) so it no longer lags behind the network POST.

Progress records (`/api/progress/<bookId>`) use the same `anchor` field plus a `progress` float (0..1) and a `fontSize` so the next device opens at the same place at the same size. They also carry `recentPages` — a rolling buffer (max 30) of `{ms: int, words: int}` samples capturing how long the user spent on recent pages and the per-page word count at that moment. The reader uses this to compute WPM / WPP / time-remaining in the bottom-bar metrics row. Two-stage outlier filter: (1) at insert time, samples outside `[readingMinSec, readingMaxSec]` (default 10s..120s, settable in the main Settings overlay → keys `ereader.settings.readingMinSec` / `ereader.settings.readingMaxSec`) are dropped as accidental swipes / phone-down time; (2) at compute time, a MAD-based modified-z-score filter (Iglewicz & Hoaglin, `|0.6745·(x−median)/MAD| > 3.5`) drops contextual outliers (epigraph / chapter-end pages) — applied to ms-per-word when a word count is available, raw ms otherwise, gated by `outlierMinSamples` (5) so tiny buffers don't trigger it. EPUB only — PDFs don't populate this. The full algorithm is also documented in `web/about.js` (user-facing).

**Unified progress for dual-format books**: For books with both ebook and audiobook, ONE progress record is stored under the Calibre book ID. The record contains both ebook-specific fields (anchor, page, fontSize, recentPages) AND audiobook-specific fields (mediaType: "audiobook", absId, position, duration). Whichever format is used updates its fields while preserving the other format's fields. The `progress` float (0..1) reflects whichever format was updated most recently. Audio-only books (no Calibre ebook) use `bookId = "abs:<absId>"`. This ensures one progress record per book regardless of format, eliminating duplicate entries in "recently read".

## Calibre Integration

- Source of truth: a Calibre Content Server (NOT the Calibre DB directly)
- The backend is a thin proxy — never bypass it to hit Calibre directly from the web reader
- Do not write to the Calibre library from this project — read-only access

## Restarting Services

Both the backend (Flask on 8091) and the web static server (8090) are bare processes, not Docker.

```bash
# Backend
kill $(cat /home/brandon/projects/Ereader/server.pid) 2>/dev/null || true
cd /home/brandon/projects/Ereader/backend && ./run.sh &
sleep 2
curl -s http://localhost:8091/api/health

# Static server (MUST run from web/ directory!)
ps aux | grep "python3 serve.py" | grep -v grep | awk '{print $2}' | xargs -r kill
cd /home/brandon/projects/Ereader/web && python3 serve.py &
sleep 1
ss -ltnp | grep :8090
```

**CRITICAL**: After ANY frontend code change, the user must **force-close the app** (swipe away from recent apps) and reopen. The Android WebView aggressively caches HTML/JS — navigating back to the library is NOT enough.

**Full recovery procedure**: See `docs/RECOVERY.md` for the complete checklist when things break.

## Testing

- `backend/run-tests.sh` runs the whole suite (stdlib `unittest`, no extra deps; uses the existing `venv`). `./run-tests.sh test_matching` runs one module. Modules live in `backend/tests/`:
  - `test_matching.py` — offline unit tests of `_norm`/`_norm_author`/`_strip_edition`/`_first_author`/`match_works` (imports `server.py`, synthetic data, no network).
  - `test_services.py` — reachability of backend :8091, static :8090, Calibre, and ABS (FAIL, not skip, when down; ABS group skips if `absEnabled` is false).
  - `test_api.py` — every `/api` read route + self-cleaning CRUD round-trips for highlights/progress.
  - `test_audiobooks.py` — play→sync→close lifecycle and the HLS proxy chain (manifest + `.ts` via the backend, asserts CORS `*` and that track URLs never point at raw ABS :13378). Skipped when ABS is off.
- HTTP tests hit the **running** services — start the backend + static server first. The old `backend/test-server.sh` is stale (hardcodes the wrong port 5000); prefer `run-tests.sh`.

## Hot-reloading the web reader

`web/reader.html` is loaded by the Android WebView from `http://100.69.184.113:8090`. Edits to `web/*.html` are live — the user refreshes by **backing out of the book to the library and re-opening it**, or by swiping the app away in the recent-apps switcher.

**Never tell the user to run `adb` commands.** The user does not have `adb` set up on their phone or laptop and will not run them. If you need to confirm a build is loaded, ask them to check the `build-stamp` pill in the status bar. If you need device-side debug output, add an on-screen toast or a visible UI hint — do not rely on `adb logcat` or `chrome://inspect`.

## Safety

- `web/ereader.apk` is a signed release artifact — don't overwrite without permission
- `.idsig` files are signing metadata — never commit deletion
- Calibre library at `/mnt/boston/...` (see shared guidelines) — read-only, never modify

## APK Delivery Flow

Debug builds are pulled over HTTP and installed in-place — never via `adb install`.

1. `./build-app.sh` (repo root) runs `./gradlew assembleDebug` in `simple-app/`, then copies `app/build/outputs/apk/debug/app-debug.apk` to `web/ereader.apk`.
2. The phone browses to `http://100.69.184.113:8090/ereader.apk` (the static server sends `Content-Disposition: attachment` + the correct `.apk` MIME) and taps the download.
3. Android treats it as an upgrade (same debug keystore as the previous build) — no uninstall, no data loss.

**Never run `./build-app.sh` or push a new APK without explicit user permission.** The user runs builds themselves so they control device install timing.

## Development Best Practices

### Safe Workflow (ALWAYS Follow This)
1. **Make ONE change at a time** - Test immediately after each change
2. **Never edit both backend and frontend simultaneously** - Change one, test, then the other
3. **Test syntax before deploying** - `node -e "require('fs').readFileSync('web/index.html')"` catches errors
4. **Commit working states frequently** - So you can `git checkout .` to escape
5. **Read the logs** - Backend: `backend/server.log`, Static server: terminal output
6. **Remind user to force-close app** - After ANY frontend change (WebView cache is sticky)

### Common Mistakes That Break Everything
- JavaScript syntax errors → entire page goes black (no visible error to user)
- Calling functions before they're defined → black screen
- Using unsupported APIs (`AbortSignal.timeout()`) → breaks in older WebViews
- Not force-closing the app → serves stale cached JS forever
- Making 10 changes and testing once → impossible to debug which one broke it

**When stuck for >5 minutes:** `git checkout .` and start over with a smaller change. See `docs/RECOVERY.md`.

### Offline Support Implementation
**DO NOT implement offline support without following `docs/OFFLINE_PLAN.md`.** The phased approach is mandatory - attempting to do it all at once will break the app.

## Agent Behavior Rules

### DO NOT GUESS — RESEARCH FIRST

When working with integrations or external systems:
1. **NEVER guess about API schemas, field names, or data structures**
2. **ALWAYS look at the actual code** in the external system's repository
3. **ALWAYS check the database schema** if you're dealing with persistent data
4. **NEVER assume** you know how something works based on naming conventions
5. **ASK the user** for clarification if documentation is unclear or missing

### Examples of Forbidden Guessing:
- ❌ "The TBR queue is probably ordered by a `rank` field"
- ❌ "Let me query for `status=in_progress` to find the next book"
- ❌ "I'll assume the format is auto-detected correctly"

### Required Research Steps:
- ✅ "Let me check the vendored GreatReads code at `greatreads/` to understand the chain structure"
- ✅ "Let me look at the database schema to see what fields exist"
- ✅ "Let me trace through the actual code flow to understand how this works"

### GreatReads Integration Specifics

When working with GreatReads integration:
- **Primary reference**: the "GreatReads Integration (upstream API contract)" section in this file
- **Source code**: vendored in-repo at `greatreads/` (was the external `../GreatReads/`)
- **Chain system docs**: `greatreads/CHAIN_SYSTEM_ANALYSIS.md`
- **ALWAYS verify** field names and data structures against the actual GreatReads schema before writing code
- **Chain structure**: Uses `id_previous` linked lists, NOT status queries or rank ordering

### Communication Style

**DO:**
- Be direct and factual
- State what you found, what you changed, and what the result should be
- Acknowledge when you made a mistake: "I was wrong about X. The actual behavior is Y."
- Present information concisely without fluff

**DO NOT:**
- Use excessive enthusiasm or emoji (one emoji per response maximum, if relevant)
- Say "Great question!", "Excellent catch!", "Perfect!", "Amazing!", etc.
- Pad responses with pleasantries or reassurances
- Act cheery when you've made an error — just fix it

**Example of GOOD communication:**
> "I was wrong. GreatReads uses a linked-list chain structure via `id_previous` fields. I've updated the code to find the next reading by looking for the one whose `id_previous` points to the current reading."

**Example of BAD communication:**
> "Great catch! 🎉 You're absolutely right! Let me fix that for you! This is going to be amazing when it works! ✨"

## Documentation Maintenance Protocol

**This file is the project's living onboarding doc.** When you make a change that affects any of the following, update the relevant section in *this* file in the same edit batch — do not wait to be asked, and do not create a separate doc to track it:

- Port numbers, hostnames, or process layout (Backend / Static server / APK)
- Any addition, removal, or signature change to a `/api/...` endpoint in `backend/server.py` → update "Backend API Surface"
- New shape or field for items in `backend/data/*.json` → update "Persisted Backend State" or "Highlight & Bookmark Model"
- New, removed, or renamed `@JavascriptInterface` method on `MainActivity.JsBridge` → update "JS ↔ Java Bridge"
- Changes to the EPUB column / pagination math in `web/reader.html` → update "EPUB Reader — Layout Invariants" (especially if you touch `calculatePages`, `columnGap`, `columnWidth`, or `SIDE_PADDING`)
- New manifest permissions, `configChanges` entries, or Activity flags in `simple-app/` → update "Project Details" → Android app bullet
- Changes to the version pipeline (`version.txt`, `gvc`, `build.gradle` version derivation, `/api/version`, `/api/build-stamp`) → update "Versioning Workflow"
- Changes to the APK build/staging flow (`build-app.sh`, `web/ereader.apk` location, signing key) → update "APK Delivery Flow"

Style rules for edits to this file:
- Keep additions terse and bullet-shaped — match the surrounding density. No marketing prose, no "we should consider…" hedging.
- Never duplicate something already documented elsewhere in the file; cross-reference instead.
- If a section becomes wrong, *fix it in place*. Do not append "UPDATE 2026-XX-XX: …" notes.
- Do **not** scatter new `*.md` docs into the repo root — this file is the canonical doc; longer-form reference docs go in `docs/`.
- If you discover the file is wrong but the user's request is unrelated, mention it once and offer to fix it; don't silently rewrite half the doc.
