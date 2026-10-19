# GreatReads audit: architecture, concurrency, stability (2026-10-19)

The first pass of the playbook (#385) applied to this repo. This is a
**snapshot**, not a tracker: every finding worth fixing becomes its own GitHub
issue, and the issue is what we trust. Line numbers are from `main` at v1.2.343.

**Method:** a read-only skim (rg plus targeted reads), about 15 tool calls. It
is not exhaustive. Findings marked *check* need confirming before anyone fixes
them.

**Verdict:** GreatReads hasn't gone off the rails. It's a working system with
real safety thinking in it: locks on pending queues, TTL caches, card-borrow
budgets, online backups. The debt is structural and comes from the #22 merge:
- two SQLite writers on a rollback journal
- a whole-table progress rewrite on every ping
- `async` routes doing sync work on the event loop
- a 4.4k-line module that calls its own process over HTTP

## Ranked findings

| # | Sev | Area | Where | Problem | Fix |
|---|-----|------|-------|---------|-----|
| 1 | high | SQLite | `database.py:12-16`, `ereader_api.py:262-269` | The DB runs in **rollback-journal mode**: `PRAGMA journal_mode` returns `delete` on the live DB (verified). The `_gr_db()` docstring claims WAL, but only `busy_timeout=5000` is set. SQLAlchemy plus raw `sqlite3` writers fight for the lock, and "database is locked" errors get swallowed (#5). | A `connect` event listener sets `journal_mode=WAL`, `busy_timeout`, `synchronous=NORMAL` and `foreign_keys=ON`. |
| 2 | high | Data | `ereader_api.py:1147-1185` `_save_progress` | Each position ping loads every progress row, rewrites the whole table (`DELETE … NOT IN (…)` plus an upsert per row) and dumps a full JSON backup. A stale in-memory map in any process can delete other books' progress. | Upsert only the changed `book_key`; drop the `NOT IN` delete. |
| 3 | high | Event loop | `main.py:551-576` `cover_thumbnail` | `async def` runs Pillow decode/resize/save on the loop. A grid of first-time thumbnails stalls every request (same class as #363). The `/app/data/covers` path is hardcoded. | Plain `def` (threadpool), or `asyncio.to_thread`; use `settings.covers_dir`. |
| 4 | high | Event loop | `main.py:475-545`, `routes/inventory.py:14-101` | About 17 `async def` routes run sync SQLAlchemy queries inline. | Make them `def`: they never await anything. |
| 5 | high | Errors | 38× `except Exception:` in `ereader_api.py`, 21 in `routes/libby.py`, more elsewhere | Swallowed errors. Together with #1, a locked DB silently drops progress and session data. | `logger.exception` / `log_event`; swallow only on purpose, and say so in the code. |
| 6 | med | Races | `libby_autofulfill_service.py:73`, `wishlist_acquire_service`, `routes/libby.py` | Three paths borrow from the same cards and only share a process-local lock around one JSON file. The read-then-borrow window allows double borrows (cf. #357). | One acquisition mutex or DB row-claim around decide → borrow → record. |
| 7 | med | N+1 | `routes/books.py:83-85` | One `Reading` query plus a `getmtime` per book in the list. | One `IN`/`EXISTS` query. |
| 8 | med | Perf | `routes/books.py:422, 468` | `db.query(Book).all()` in request handlers (*check* how often they run). | Select columns, `yield_per`. |
| 9 | med | Perf | `ereader_api.py:1339, 1445` | Per-book Calibre metadata is fetched in a sequential loop (~3 s). Every import invalidates the cache, so it's paid again. | `ThreadPoolExecutor` plus a shared `requests.Session`, or read `metadata.db` read-only. |
| 10 | med | Event loop | `ereader_api.py` (14 `async def`, 19 `requests.*`, `time.sleep(0.5)` at :3053) | Sync HTTP and sleep are safe only from `def` routes (*check* the 14). | Audit; `httpx.AsyncClient` or `to_thread`. |
| 11 | med | Timeouts | `news_service.py:632,651`, `cover_upgrade_service.py:63` | Calls have per-request timeouts but no per-run budget in batch jobs. | Per-run deadline; a small polite pool. |
| 12 | med | Migrations | `database.py:48-72` `_ensure_columns`, `ereader_api.py:271-331` | `ALTER`/`CREATE TABLE` at every startup, no schema version, no backup first. The raw tables are invisible to `models/`. | Alembic (already a dependency) or a `schema_version` table, plus a `.backup` before any `ALTER`. |
| 13 | med | Dead code | `backend/app.py` (2.5k lines), `run.sh`, `match_audit.py` | The retired #22 copy can still be started and become a second writer. | Delete; move `build_summaries.py` to `scripts/`. |
| 14 | med | Hygiene | `routes/library.py.backup`, `templates/library.html.backup` | Tracked backup files (verified with `git ls-files`). | `git rm`; history keeps them. |
| 15 | med | God file | `ereader_api.py` (4392 lines) | Calibre/ABS clients, matching, progress, highlights, HLS proxy, APK hashing, all in one file. It calls its own process over HTTP (`GREATREADS_URL`, e.g. :4009, :4104). | Split into `routers/` + `clients/`; call services directly. |
| 16 | med | Layering | `ereader_api.py:1186+` `_gr_set_current_percent` | Raw SQL duplicates the ORM's progress-write rules by hand. | One `reading_service.set_progress()`. |
| 17 | med | Duplication | `ereader_api.py:1700,1708`, `contributor_service.py:23`, `news_service.py:286`, `wishlist_acquire_service.py:191+`, `routes/libby.py`, `routes/news.py:29` | At least five title/author normalizers. #383 and #148 were this exact divergence. | One `matching.py` module with property tests. |
| 18 | med | Layering | `libby_autofulfill_service.py:40` | A service imports private helpers from `routes.libby`. | Move the matcher into services (pairs with #17). |
| 19 | med | State | `_sync_map_cache`, `_abs_cache`, `_cover_cache`, `_auto_cache`, `_CACHE`… | Unbounded module-level dicts, some without locks. | `cachetools.TTLCache` + lock, or `lru_cache(maxsize)`. |
| 20 | med | Races | `main.py:105-230` watcher + `_auto_sync` | The watcher fires on any `.db`/`.sqlite` event (including -wal/-shm); the watch sync and the interval sync may overlap (*check*). | One sync lock; ignore sidecar files. |
| 21 | med | Indexes | `models/` | No visible indexes on `read.book_id` or `external_imports(source, external_id)`, which are hit on every progress ping (*check*). | Add the indexes; `EXPLAIN QUERY PLAN`. |
| 22 | low-med | Frontend | `web/reader.html` 8.8k, `templates/*.html` 2–2.7k each | Large copy-pasted inline scripts; uncacheable and unlinted; the APK bundles its own copy. | Extract shared `static/js/*` in small slices. |
| 23 | low-med | Robustness | `routes/libby.py:653` `_bg_downloads` | In-flight downloads tracked in memory; a rebuild loses them, half-written files may remain. | Persist state; write to a temp file, then rename. |
| 24 | low | Ops | `web/keep-alive.sh` | `git pull`s main every ~60 s and restarts unattended, with no gate. | Retire with #8. |
| 25 | low | Ops | `web/serve.py` | Possibly single-threaded (*check*); a slow proxy request blocks static files. | Retire with #8. |
| 26 | low | Observability | `services/job_status.py:21-54` | Job status is in-memory and resets on restart. | Persist to `event_log`. |
| 27 | low | Logging | `ereader_api.py:1105-1135` | `print()` with emoji instead of `logger`. | `logger`. |
| 28 | low | Startup | `main.py:272-340` | Interval lookups silently fall back to env defaults on any error. | Log the fallback. |
| 29 | low | Perf | `routes/books.py:1138-1183` | Autocomplete runs `SELECT DISTINCT` over whole columns on every keystroke. | `LIKE 'x%'` + `LIMIT`, or a cache. |

## Highest value per hour
1. WAL + `busy_timeout` on the engine (#1). A one-time online backup first.
2. Per-key progress upsert (#2).
3. `async def` → `def` for routes that never await (#3, #4).
4. Log instead of swallowing (#5).
5. One acquisition lock across borrow paths (#6), **before** auto-borrow scales.
6. One `matching.py` (#17, #18), the source of the recurring duplicate bugs (#15).
7. Delete `backend/app.py` + the `.backup` files (#13, #14).
