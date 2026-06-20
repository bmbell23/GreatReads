#!/usr/bin/env python3
"""
Ereader Backend Server
Serves ebook files from Calibre Content Server via REST API
"""

from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import requests
import os
import json
import sqlite3
import uuid
import threading
import time
import re
import unicodedata
from collections import defaultdict

# Absorbed from backend/app.py (:8091) into the GreatReads process (#22).
# Routes already carry their full /api/... paths; included with no prefix in
# main.py. CORS + uvicorn are owned by the parent app.
router = APIRouter()


# Calibre Content Server configuration
CALIBRE_URL = os.environ.get('CALIBRE_URL', 'http://localhost:8083')
CALIBRE_LIBRARY = os.environ.get('CALIBRE_LIBRARY', 'library')

# Audiobookshelf (ABS) configuration — optional second backend source for
# audiobooks. When ABS_URL/ABS_TOKEN are unset (or ABS is unreachable at
# request time) every ABS code path degrades to an empty result, so the
# merged /api/library falls back to the Calibre-only list and the ebook
# experience is untouched. See the "Audiobook Integration" spec (Requests).
ABS_URL = os.environ.get('ABS_URL', '').rstrip('/')
ABS_TOKEN = os.environ.get('ABS_TOKEN', '')
ABS_LIBRARY_ID = os.environ.get('ABS_LIBRARY_ID', '')
ABS_ENABLED = bool(ABS_URL and ABS_TOKEN)
# Host the *client* (phone WebView) uses to reach ABS media/HLS directly. The
# backend talks to ABS over localhost, but the WebView is remote, so playback
# track URLs must point at a reachable host. Defaults to ABS_URL with a
# localhost/127.0.0.1 host swapped for the Tailscale IP (matches PUBLIC_HOST).
ABS_PUBLIC_URL = os.environ.get('ABS_PUBLIC_URL', '').rstrip('/')
if not ABS_PUBLIC_URL and ABS_URL:
    ABS_PUBLIC_URL = re.sub(r'//(localhost|127\.0\.0\.1)\b', '//100.69.184.113', ABS_URL)
# host:port the phone uses to reach THIS backend. Used to build absolute cover
# URLs and the HLS-proxy URLs we hand back in playback sessions.
PUBLIC_HOST = os.environ.get('PUBLIC_HOST', '100.69.184.113:8091')

# GreatReads reading-tracker integration (optional). When asked to (an explicit
# POST /api/greatreads/sync — never automatically), we mirror our in-progress
# reading percentages into the GreatReads tracker. The push RESPECTS the format
# GreatReads already tracks per book (its reading `media`): an "Audio" reading
# is updated from our audiobook percentage, an "Ebook" reading from our ebook
# percentage. We only ever update the percentage of existing in-progress
# readings — we never create, finish, or re-format anything.
# Canonical GreatReads service: the local vendored instance on :8092 (Story 2).
# It reads/writes the repo-local greatreads.db that the reader also writes to, so
# there is one source of truth. The old remote prod (:8007) has been retired.
GREATREADS_URL = os.environ.get('GREATREADS_URL', 'http://127.0.0.1:8092').rstrip('/')

# Persisted user data (highlights + bookmarks). Single JSON file on disk —
# trivial to back up, trivial to grep. Guarded by a lock because the server
# may handle concurrent requests across worker threads.
DATA_DIR = os.environ.get('EREADER_DATA_DIR',
                          os.path.join(os.path.dirname(__file__), 'data'))
os.makedirs(DATA_DIR, exist_ok=True)
HIGHLIGHTS_FILE = os.path.join(DATA_DIR, 'highlights.json')
_highlights_lock = threading.Lock()
# Auto-bookmarks are an "auto-save" of the user's place — we only need the
# most recent N per book. Older ones are pruned on every new auto-bookmark
# create (see /api/highlights POST). Manual / line bookmarks are unbounded.
# Applies per bookId, so the ebook (Calibre id) and audiobook (abs:<id>) sides
# of a dual-format work each keep their own most-recent N.
AUTO_BOOKMARK_LIMIT_PER_BOOK = 10

# Per-book reading progress (last anchor / page / fraction). Same file-on-disk
# pattern as highlights. Shape on disk: { "<bookId>": {progress dict}, ... }
PROGRESS_FILE = os.path.join(DATA_DIR, 'progress.json')
_progress_lock = threading.Lock()

# Manual audiobook<->ebook links for the edge cases auto-matching misses
# (multi-part sets whose parts don't share a title/author, divergent author
# spellings, etc). Optional — absent file means "no manual overrides". Three
# accepted value shapes per Calibre id (see _normalize_links):
#   "573": "<absId>"                         -> one edition, parts=[absId]
#   "573": ["<absId1>", "<absId2>"]          -> one edition, those ordered parts
#   "573": {"editions": [                      -> full control: many editions,
#       {"kind": "dramatized",                   each with kind/label + ordered
#        "label": "Dramatized Audiobook",        part absIds
#        "parts": ["<absId1>", "<absId2>"]}]}
# Generate/audit entries with `python3 match_audit.py`.
LINKS_FILE = os.path.join(DATA_DIR, 'links.json')
_links_lock = threading.Lock()

# Per-book series overrides for cases Calibre/ABS can't express and we can't
# write back to (read-only sources). Optional — absent file means "no
# overrides". Keyed by bookId (Calibre numeric id as string, or "abs:<id>").
# Value shapes:
#   "681": {"series_index": null}      -> numberless: sorts first, no badge
#   "681": {"series": "X", "series_index": 2}  -> force both
#   "681": 2                            -> force just the index
#   "681": null                         -> numberless (shorthand)
# mtime-cached so a hand-edit is picked up without a restart, but we don't
# re-read the file on every get_book_metadata call.
SERIES_OVERRIDES_FILE = os.path.join(DATA_DIR, 'series_overrides.json')
_series_overrides_lock = threading.Lock()
_series_overrides_cache = {'mtime': None, 'data': {}}

# Per-book universe (saga) overrides — lets us assign books to a saga without
# touching the read-only Calibre/ABS sources. Keyed by bookId (str or "abs:<id>"),
# value is the saga name string (e.g. "Maasverse"). mtime-cached same as series_overrides.
UNIVERSE_OVERRIDES_FILE = os.path.join(DATA_DIR, 'universe_overrides.json')
_universe_overrides_lock = threading.Lock()
_universe_overrides_cache = {'mtime': None, 'data': {}}

# Chapter-summary sets (e.g. the Malazan compendium). These are committed
# reference assets, NOT runtime state, so they live in backend/summaries/ (a
# tracked dir) rather than DATA_DIR. Each <id>.json is produced by
# build_summaries.py and shaped {id, title, source, books:[{title, chapters:
# [{title, html}]}]}. Loaded once, indexed by normalized book title so the
# reader can resolve a Calibre book → its summary book section. See
# /api/summaries/<bookId>.
SUMMARIES_DIR = os.environ.get('EREADER_SUMMARIES_DIR',
                               os.path.join(os.path.dirname(__file__), 'summaries'))
# Optional manual book→summary overrides for titles that don't match by name.
# Keyed by Calibre bookId (str). Value: {"set": "<setId>", "book": "<bookTitle>"}.
SUMMARY_LINKS_FILE = os.path.join(DATA_DIR, 'summary_links.json')
_summaries_lock = threading.Lock()
# Cache: signature (sorted (file, mtime) tuples) → built index, so editing or
# adding a set JSON is picked up without a restart.
_summaries_cache = {'sig': None, 'sets': {}, 'book_index': {}}


# Single source of truth for the app version, bumped by `gvc` (see
# ../dotfiles/bashrc/conf.d/20-functions.sh — gvc auto-increments the
# patch number in version.txt, commits, tags, pushes). The frontend
# fetches /api/version on load and the Android build.gradle reads the
# same file at build time, so a single `gvc <msg>` keeps the web pill
# and APK versionName in sync.
VERSION_FILE = os.environ.get('EREADER_VERSION_FILE',
                              os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                           'version.txt'))

def _read_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip() or '0.0.0'
    except OSError:
        return '0.0.0'

# Highlights/bookmarks now live in the GreatReads SQLite DB (Story 3), same as
# progress — one store, covered by the daily DB backup. highlights.json is still
# written as a best-effort backup/fallback and auto-migrated on first load. These
# rows are Ereader-private (GreatReads doesn't read them). See _load_highlights.
def _load_highlights_json():
    if not os.path.exists(HIGHLIGHTS_FILE):
        return []
    try:
        with open(HIGHLIGHTS_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️  Could not load highlights JSON: {e}")
        return []

def _load_highlights():
    """Return [record]. Reads the DB; lazily migrates a legacy highlights.json
    into the table; falls back to JSON if the DB is unavailable."""
    try:
        conn = _gr_db()
        try:
            _ensure_highlights_table(conn)
            rows = conn.execute('SELECT data FROM ereader_highlights').fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r['data']))
            except Exception:
                pass
        if out:
            return out
        # Table empty → one-time migration from the legacy JSON file (if any).
        legacy = _load_highlights_json()
        if legacy:
            print(f"Migrating {len(legacy)} highlight(s) from JSON into the DB…")
            _save_highlights(legacy)
        return legacy
    except Exception as e:
        print(f"⚠️  Highlights DB unavailable, using JSON fallback: {e}")
        return _load_highlights_json()

def _save_highlights(items):
    """Persist the full highlight/bookmark list. The DB is primary; the JSON file
    is also written as a best-effort backup so a save can't lose data."""
    try:
        conn = _gr_db()
        try:
            _ensure_highlights_table(conn)
            with conn:
                ids = [str(it['id']) for it in items if it.get('id')]
                if ids:
                    ph = ','.join('?' * len(ids))
                    conn.execute(
                        f'DELETE FROM ereader_highlights WHERE id NOT IN ({ph})', ids)
                else:
                    conn.execute('DELETE FROM ereader_highlights')
                for it in items:
                    iid = it.get('id')
                    if not iid:
                        continue
                    bid = it.get('bookId')
                    conn.execute(
                        'INSERT INTO ereader_highlights(id,book_id,data,created) '
                        'VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                        'book_id=excluded.book_id, data=excluded.data, created=excluded.created',
                        (str(iid), str(bid) if bid is not None else None,
                         json.dumps(it), it.get('created') or 0))
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️  Highlights DB write failed (JSON backup still written): {e}")
    try:
        tmp = HIGHLIGHTS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(items, f, indent=2)
        os.replace(tmp, HIGHLIGHTS_FILE)
    except Exception as e:
        print(f"⚠️  Highlights JSON backup write failed: {e}")

# ── Progress store (GreatReads SQLite DB, with JSON backup) ───────────────
# Reading progress now lives in the GreatReads SQLite DB — one source of truth,
# co-located with the data GreatReads serves. We keep writing progress.json too,
# as a best-effort backup/fallback, so the reader can never lose position if the
# DB is briefly unavailable. NOTE: GreatReads does NOT read the ereader_progress
# table — it reads read.current_percent, which we set directly at save time via
# _gr_set_current_percent(). That is what makes the old title-matching "sync" job
# obsolete: progress is written straight to where GreatReads reads it.
GREATREADS_DB = os.environ.get('GREATREADS_DB', os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 '..', 'greatreads', 'data', 'greatreads.db')))

def _gr_db():
    """Open the GreatReads SQLite DB (shared read/write). WAL + busy_timeout so we
    coexist cleanly with the GreatReads container's own connections."""
    conn = sqlite3.connect(GREATREADS_DB, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    return conn

def _ensure_progress_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS ereader_progress ('
        ' book_key TEXT PRIMARY KEY,'
        ' data     TEXT NOT NULL,'   # full JSON progress record (reader/player contract)
        ' progress REAL,'            # 0..1 fraction (denormalized for quick queries)
        ' updated  INTEGER)')        # epoch ms (denormalized for ORDER BY)

def _ensure_highlights_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS ereader_highlights ('
        ' id      TEXT PRIMARY KEY,'
        ' book_id TEXT,'             # denormalized for per-book queries
        ' data    TEXT NOT NULL,'    # full JSON highlight/bookmark record
        ' created INTEGER)')         # epoch ms (denormalized for ORDER BY)

# ---------- Small app-wide key/value store (JSON values) ----------
# Used for cross-book singletons that don't belong in ereader_progress (which the
# library iterates per book). Currently: the global reading-speed baseline (#29).
def _ensure_app_kv_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS ereader_app_kv ('
        ' key   TEXT PRIMARY KEY,'
        ' value TEXT NOT NULL)')     # JSON-encoded value

def _kv_get(key, default=None):
    try:
        conn = _gr_db()
        try:
            _ensure_app_kv_table(conn)
            row = conn.execute('SELECT value FROM ereader_app_kv WHERE key=?', (key,)).fetchone()
        finally:
            conn.close()
        if row:
            try:
                return json.loads(row['value'])
            except Exception:
                return default
        return default
    except Exception:
        return default

def _kv_set(key, value):
    try:
        conn = _gr_db()
        try:
            _ensure_app_kv_table(conn)
            with conn:
                conn.execute(
                    'INSERT INTO ereader_app_kv(key,value) VALUES(?,?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key, json.dumps(value)))
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️  KV write failed for {key}: {e}")

# Global cross-book reading-speed baseline (#29). ms-per-word is layout-invariant,
# so it transfers across books/devices. EMA so it tracks the user's pace over time.
_READING_SPEED_KEY = 'reading_speed_baseline'
_RS_ALPHA = 0.2  # weight of each fresh session estimate

def _update_reading_baseline(ms_per_word):
    """Fold a fresh ebook ms-per-word estimate (the reader's current real avg)
    into the global baseline via a simple EMA."""
    try:
        mpw = float(ms_per_word)
    except (TypeError, ValueError):
        return
    # Sanity gate: reject junk. ~3 ms/word ≈ 20k WPM (impossible); 60s/word is a
    # sane upper bound for real reading.
    if not (3.0 <= mpw <= 60000.0):
        return
    cur = _kv_get(_READING_SPEED_KEY) or {}
    old = cur.get('ebook_ms_per_word')
    new = ((1 - _RS_ALPHA) * old + _RS_ALPHA * mpw) if isinstance(old, (int, float)) and old > 0 else mpw
    cur['ebook_ms_per_word'] = round(new, 2)
    cur['samples'] = int(cur.get('samples') or 0) + 1
    cur['updated'] = int(time.time() * 1000)
    _kv_set(_READING_SPEED_KEY, cur)

def _load_progress_json():
    if not os.path.exists(PROGRESS_FILE):
        return {}
    try:
        with open(PROGRESS_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"⚠️  Could not load progress JSON: {e}")
        return {}

def _load_progress():
    """Return {book_key: record}. Reads the DB; lazily migrates a legacy
    progress.json into the table; falls back to JSON if the DB is unavailable."""
    try:
        conn = _gr_db()
        try:
            _ensure_progress_table(conn)
            rows = conn.execute('SELECT book_key, data FROM ereader_progress').fetchall()
        finally:
            conn.close()
        out = {}
        for r in rows:
            try:
                out[r['book_key']] = json.loads(r['data'])
            except Exception:
                pass
        if out:
            return out
        # Table empty → one-time migration from the legacy JSON file (if any).
        legacy = _load_progress_json()
        if legacy:
            print(f"Migrating {len(legacy)} progress record(s) from JSON into the DB…")
            _save_progress(legacy)
        return legacy
    except Exception as e:
        print(f"⚠️  Progress DB unavailable, using JSON fallback: {e}")
        return _load_progress_json()

def _save_progress(data):
    """Persist the full {book_key: record} map. The DB is the primary store; the
    JSON file is also written as a best-effort backup so the reader can't lose
    position even if the DB write fails."""
    # Primary: sync the table to match `data` in one transaction.
    try:
        conn = _gr_db()
        try:
            _ensure_progress_table(conn)
            with conn:
                keys = [str(k) for k in data.keys()]
                if keys:
                    ph = ','.join('?' * len(keys))
                    conn.execute(
                        f'DELETE FROM ereader_progress WHERE book_key NOT IN ({ph})', keys)
                else:
                    conn.execute('DELETE FROM ereader_progress')
                for k, rec in data.items():
                    frac = rec.get('progress')
                    conn.execute(
                        'INSERT INTO ereader_progress(book_key,data,progress,updated) '
                        'VALUES(?,?,?,?) ON CONFLICT(book_key) DO UPDATE SET '
                        'data=excluded.data, progress=excluded.progress, updated=excluded.updated',
                        (str(k), json.dumps(rec),
                         frac if isinstance(frac, (int, float)) else None,
                         rec.get('updated') or 0))
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️  Progress DB write failed (JSON backup still written): {e}")
    # Backup: keep the legacy JSON file current as a fallback store.
    try:
        tmp = PROGRESS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, PROGRESS_FILE)
    except Exception as e:
        print(f"⚠️  Progress JSON backup write failed: {e}")

def _gr_set_current_percent(book_key, record):
    """Write a book's reading % straight into GreatReads (read.current_percent) for
    the matching in-progress reading, resolved precisely via external_imports — no
    title matching, no batch sync. Replicates GreatReads' own progress-write fields
    (current_percent + manual_override + date_progress_set). Best-effort; never
    raises, so a progress save is never blocked by GreatReads being unavailable."""
    try:
        frac = record.get('progress')
        if not isinstance(frac, (int, float)) or frac <= 0 or frac >= 1:
            return  # only meaningful for an in-progress 0<pct<100
        pct = round(frac * 100, 1)
        bk = str(book_key)
        if bk.startswith('abs:'):
            source, ext_id, media = 'audiobookshelf', bk[4:], 'Audio'
        else:
            source, ext_id = 'calibre', bk
            media = 'Audio' if record.get('mediaType') == 'audiobook' else 'Ebook'
        conn = _gr_db()
        try:
            row = conn.execute(
                'SELECT r.id, r.current_percent FROM read r '
                'JOIN external_imports ei ON ei.book_id = r.book_id '
                'WHERE ei.source=? AND ei.external_id=? AND r.media=? '
                '  AND r.date_started IS NOT NULL AND r.date_finished_actual IS NULL '
                'ORDER BY r.date_started DESC LIMIT 1',
                (source, ext_id, media)).fetchone()
            if not row:
                return  # GreatReads has no in-progress reading for this book+format
            cur = row['current_percent']
            if cur is not None and abs(float(cur) - pct) < 0.05:
                return  # unchanged — skip a redundant write (keep GR tracking tightly)
            from datetime import datetime
            with conn:
                conn.execute(
                    'UPDATE read SET current_percent=?, current_percent_manual_override=1, '
                    'date_progress_set=? WHERE id=?',
                    (pct, datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'), row['id']))
        finally:
            conn.close()
        # Progress changed → ask GreatReads to recompute estimated end dates and the
        # rest of this format's chain. Best-effort + backgrounded so a page-turn save
        # never blocks on it.
        _gr_recalculate_chains_async()
    except Exception as e:
        print(f'GreatReads current_percent update failed for {book_key}: {e}')

def _gr_recalculate_chains_async():
    """Fire-and-forget POST to GreatReads' chain recalculation. Never raises."""
    def _go():
        try:
            requests.post(f'{GREATREADS_URL}/api/chains/recalculate', timeout=8)
        except Exception as e:
            print(f'GreatReads chain recalc failed: {e}')
    threading.Thread(target=_go, daemon=True).start()

def get_calibre_books(limit=None, offset=0, query=None):
    """Fetch books from Calibre Content Server"""
    # Serve from the short-TTL memo if warm (see _calibre_books_cache). limit
    # None and 0 both mean "all" (num=1000 below), so normalize them to one key.
    _ck = (limit or 0, offset, query or '')
    _now = time.time()
    with _calibre_books_lock:
        _hit = _calibre_books_cache.get(_ck)
        if _hit and _now - _hit[2] < _CALIBRE_BOOKS_TTL:
            return _hit[0], _hit[1]
    try:
        params = {
            'library_id': CALIBRE_LIBRARY,
            'num': limit if limit else 1000,
            'offset': offset,
            'sort': 'author'  # Sort by author in Calibre
        }
        if query:
            params['query'] = query

        print(f"Fetching books with params: {params}")
        response = requests.get(f'{CALIBRE_URL}/ajax/search', params=params, timeout=30)
        response.raise_for_status()
        search_data = response.json()

        print(f"Calibre returned {len(search_data.get('book_ids', []))} book IDs, total: {search_data.get('total_num', 0)}")

        books = []
        for book_id in search_data.get('book_ids', []):
            book_data = get_book_metadata(book_id)
            if book_data:
                books.append(book_data)

        print(f"Successfully loaded {len(books)} books")

        # Additional sorting: by author last name, then series, then published date
        def sort_key(book):
            # Extract last name from first author
            author = book.get('author', 'Unknown')
            last_name = author.split(',')[0] if ',' in author else author.split()[-1] if author.split() else 'Unknown'

            # Series with index, or empty (handle None)
            series = book.get('series') or ''
            series_index = book.get('series_index') or 0

            # Publication date (handle None)
            published = book.get('published') or ''

            return (last_name.lower(), series.lower(), series_index, published)

        books.sort(key=sort_key)

        print(f"Returning {len(books)} sorted books")

        total = search_data.get('total_num', 0)
        with _calibre_books_lock:
            _calibre_books_cache[_ck] = (books, total, _now)
        return books, total
    except Exception as e:
        print(f"Error fetching books from Calibre: {e}")
        import traceback
        traceback.print_exc()
        return [], 0

def get_book_metadata(book_id):
    """Get metadata for a specific book from Calibre"""
    try:
        response = requests.get(
            f'{CALIBRE_URL}/ajax/book/{book_id}/{CALIBRE_LIBRARY}',
            timeout=10
        )
        response.raise_for_status()
        book = response.json()

        # Extract relevant information
        authors = book.get('authors', ['Unknown'])
        formats = book.get('formats', [])

        # Get external-facing URL (replace localhost with actual host)
        host = os.environ.get('PUBLIC_HOST', '100.69.184.113:8091')

        # Calibre's #word_count custom column ("Words" in the user's library)
        # — exposed so the reader can compute reading-speed estimates without
        # walking the full text DOM. May be None for older imports; the reader
        # treats null as "no WPM display".
        word_count = None
        try:
            word_count = book.get('user_metadata', {}).get('#word_count', {}).get('#value#')
        except (AttributeError, TypeError):
            pass

        # Optional Calibre custom column "#asin" — when present it's the most
        # reliable key for matching this work to an Audiobookshelf item (ASIN
        # is stable; audiobook ISBNs are frequently null/wrong). Read the same
        # way as #word_count; stays None when the column doesn't exist.
        asin = None
        try:
            asin = book.get('user_metadata', {}).get('#asin', {}).get('#value#')
        except (AttributeError, TypeError):
            pass

        # Optional Calibre custom column "#universe" (label "Universe") — the
        # over-arching meta-collection a book belongs to (e.g. "The Cosmere",
        # "Realm of the Elderlings"). Powers the Saga grouping. Read the same
        # way as #asin; stays '' when the column is unset. ABS has no equivalent.
        universe = None
        try:
            universe = book.get('user_metadata', {}).get('#universe', {}).get('#value#')
        except (AttributeError, TypeError):
            pass

        # Cache-bust token: Calibre bumps `last_modified` whenever a book (or its
        # cover) is edited. Folding it into the cover URL means the frontend's
        # per-URL IndexedDB blob cache + the 30-day HTTP cache self-invalidate
        # when you replace cover art in Calibre — otherwise the old cover would
        # be served indefinitely. Just the digits, e.g. "20260326155759".
        cover_v = ''.join(c for c in (book.get('last_modified') or '') if c.isdigit())
        thumb_q = f'?type=thumb&v={cover_v}' if cover_v else '?type=thumb'
        cover_q = f'?v={cover_v}' if cover_v else ''

        return _apply_series_override({
            'id': str(book_id),
            'title': book.get('title', 'Unknown'),
            'authors': authors,
            'author': ', '.join(authors),
            'publisher': book.get('publisher', ''),
            'formats': formats,
            'format': formats[0].upper() if formats else 'UNKNOWN',
            'tags': book.get('tags', []),
            'series': book.get('series', ''),
            'series_index': book.get('series_index', 0),
            'thumbnail': f'http://{host}/api/ebooks/{book_id}/cover{thumb_q}',
            'cover': f'http://{host}/api/ebooks/{book_id}/cover{cover_q}',
            'description': book.get('comments', ''),
            'isbn': book.get('isbn', ''),
            'asin': asin or '',
            'universe': universe or '',
            'published': book.get('pubdate', ''),
            'rating': book.get('rating', 0),
            'wordCount': word_count,
        })
    except Exception as e:
        print(f"Error fetching book {book_id}: {e}")
        return None

# ---------------------------------------------------------------------------
# Audiobookshelf (ABS) integration — optional second source for audiobooks.
# Every network call is wrapped so any failure (not configured, timeout, auth,
# ABS down) returns an empty result. /api/library then serves the Calibre-only
# list and never 500s. See "Audiobook Integration" + "Migration & Safety" in
# the in-app Requests doc for the full design.
# ---------------------------------------------------------------------------

# Resolved ABS library id is cached after first lookup (it doesn't change at
# runtime). Reset only on process restart.
_abs_cache = {'library_id': None}

# --- Full-library match cache -----------------------------------------------
# /api/library used to run match_works() on a single Calibre page at a time,
# which caused two problems: (1) audiobooks whose ebook was on a later page
# appeared as bogus "audio-only" duplicates on page 0, and (2) audio-only items
# (e.g. newly downloaded books with no Calibre ebook yet) were missed entirely
# because the consumed set was built from the wrong slice.
# Fix: run the full Calibre×ABS match once, cache the result, then serve each
# page by looking up Calibre books in the pre-built enrichment map.
_library_cache_lock = threading.Lock()
_library_cache: dict = {
    'enrich_map': None,   # {str(calibre_id): merged_item} for ebook items
    'audio_only': None,   # [merged_item] for ABS items with no Calibre match
    'ts': 0.0,
}
_LIBRARY_CACHE_TTL = 120  # seconds — rebuilt when new books are detected

# Short-TTL memo for the raw Calibre fetch. get_calibre_books() pulls per-book
# metadata in a sequential loop, so an unpaginated browse costs ~3s; without
# this it re-ran on EVERY /api/library hit (the merge cache above only covered
# the ABS×Calibre match, not the underlying Calibre fetch). Keyed on the query
# shape (num, offset, query); same 120s window as the merge cache, and busted
# immediately by _invalidate_library_caches() when a new book is imported.
_calibre_books_lock = threading.Lock()
_calibre_books_cache: dict = {}   # (num, offset, query) -> (books, total, ts)
_CALIBRE_BOOKS_TTL = 120  # seconds

def _invalidate_library_caches():
    """Drop the Calibre fetch memo and force the next merge rebuild. Call after
    a known library mutation (e.g. a fresh Calibre import) so the change shows
    up immediately instead of waiting out the TTL."""
    with _calibre_books_lock:
        _calibre_books_cache.clear()
    with _library_cache_lock:
        _library_cache['ts'] = 0.0

def _abs_headers():
    return {'Authorization': f'Bearer {ABS_TOKEN}'}

def _abs_get(path, params=None, timeout=15):
    """GET against ABS, returning parsed JSON or None on any failure. No-op
    (None) when ABS isn't configured."""
    if not ABS_ENABLED:
        return None
    try:
        r = requests.get(f'{ABS_URL}{path}', headers=_abs_headers(),
                         params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"⚠️  ABS GET {path} failed: {e}")
        return None

def _abs_post(path, json_body=None, timeout=20):
    """POST against ABS, returning parsed JSON ({} when the body is empty, as
    /session/.../close and /sync do) or None on any failure. No-op (None) when
    ABS isn't configured."""
    if not ABS_ENABLED:
        return None
    try:
        r = requests.post(f'{ABS_URL}{path}', headers=_abs_headers(),
                          json=(json_body or {}), timeout=timeout)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', '')
        if r.content and ct.startswith('application/json'):
            return r.json()
        return {}
    except Exception as e:
        print(f"⚠️  ABS POST {path} failed: {e}")
        return None

def get_abs_library_id():
    """Resolve which ABS library to surface: honour ABS_LIBRARY_ID, else the
    first library whose mediaType is 'book', else the first one. Cached."""
    if not ABS_ENABLED:
        return None
    if ABS_LIBRARY_ID:
        return ABS_LIBRARY_ID
    if _abs_cache['library_id']:
        return _abs_cache['library_id']
    data = _abs_get('/api/libraries')
    if not data:
        return None
    libs = data.get('libraries', []) if isinstance(data, dict) else (data or [])
    chosen = None
    for lib in libs:
        if lib.get('mediaType') == 'book':
            chosen = lib.get('id')
            break
    if not chosen and libs:
        chosen = libs[0].get('id')
    _abs_cache['library_id'] = chosen
    return chosen

def normalize_abs_item(raw):
    """Map an ABS LibraryItem onto the same dict shape get_book_metadata()
    returns, plus audiobook-specific fields. Returns None for non-book items
    or on any parse error. Handles both minified (list) and expanded items."""
    try:
        if raw.get('mediaType') != 'book':
            return None
        media = raw.get('media', {}) or {}
        meta = media.get('metadata', {}) or {}
        abs_id = raw.get('id')
        if not abs_id:
            return None
        host = PUBLIC_HOST

        # authors: [{id,name}] (expanded) or authorName (minified)
        authors = []
        if isinstance(meta.get('authors'), list):
            authors = [a.get('name') for a in meta['authors'] if a.get('name')]
        if not authors and meta.get('authorName'):
            authors = [meta['authorName']]
        if not authors:
            authors = ['Unknown']

        # series: [{name,sequence}] (expanded) or seriesName (minified). ABS
        # commonly bakes the sequence into the name itself ("Dresden Files
        # #10.4"); _split_abs_series peels it off so audio-only items group
        # under the same base series as their Calibre ebook counterparts.
        series, series_index = '', 0
        if isinstance(meta.get('series'), list) and meta['series']:
            s0 = meta['series'][0]
            series, seq_from_name = _split_abs_series(s0.get('name', '') or '')
            try:
                series_index = float(s0.get('sequence') or 0)
            except (TypeError, ValueError):
                series_index = 0
            if not series_index and seq_from_name is not None:
                series_index = seq_from_name
        elif meta.get('seriesName'):
            series, seq_from_name = _split_abs_series(meta['seriesName'])
            if seq_from_name is not None:
                series_index = seq_from_name

        narrators = meta.get('narrators') or []
        if not narrators and meta.get('narratorName'):
            narrators = [meta['narratorName']]

        raw_title = meta.get('title') or 'Unknown'
        # Strip edition/format markers for display (Unabridged, Dramatized
        # Adaptation, Part N of M, leading track numbers, etc.).  The raw
        # title is preserved in _rawTitle so _group_editions can still detect
        # multi-part sets whose part marker sits inside parentheses.
        clean_title = re.sub(r'\s+', ' ', _strip_edition(raw_title)).strip() or raw_title

        # Cache-bust token: ABS bumps `updatedAt` (epoch ms) when an item or its
        # cover changes. Same rationale as the Calibre cover token above — keeps
        # the frontend IndexedDB/HTTP cover caches from pinning a stale cover.
        cover_v = ''.join(c for c in str(raw.get('updatedAt') or '') if c.isdigit())
        thumb_q = f'?type=thumb&v={cover_v}' if cover_v else '?type=thumb'
        cover_q = f'?v={cover_v}' if cover_v else ''

        return _apply_series_override({
            'id': f'abs:{abs_id}',
            'absId': abs_id,
            'title': clean_title,
            '_rawTitle': raw_title,
            'authors': authors,
            'author': ', '.join(authors),
            'publisher': meta.get('publisher') or '',
            'formats': [],
            'format': 'AUDIO',
            'tags': meta.get('genres', []) or [],
            'series': series,
            'series_index': series_index,
            'thumbnail': f'http://{host}/api/audiobooks/{abs_id}/cover{thumb_q}',
            'cover': f'http://{host}/api/audiobooks/{abs_id}/cover{cover_q}',
            'audioCover': f'http://{host}/api/audiobooks/{abs_id}/cover{cover_q}',
            'description': meta.get('description') or '',
            'isbn': meta.get('isbn') or '',
            'asin': meta.get('asin') or '',
            'published': meta.get('publishedYear') or meta.get('publishedDate') or '',
            'rating': 0,
            'wordCount': None,
            'mediaTypes': ['audiobook'],
            'narrators': narrators,
            'audiobook': {
                'duration': media.get('duration'),
                'chapters': media.get('chapters') or [],
            },
        })
    except Exception as e:
        print(f"⚠️  normalize_abs_item failed: {e}")
        return None

def get_abs_items(limit=None, offset=0):
    """Fetch audiobooks from the configured ABS library as normalized dicts.
    Returns [] on any failure. mediaType filtered to 'book' in normalize."""
    lib_id = get_abs_library_id()
    if not lib_id:
        return []
    # limit=0 asks ABS for the full set; we merge/paginate on our side.
    data = _abs_get(f'/api/libraries/{lib_id}/items', params={'limit': 0})
    if not data:
        return []
    results = data.get('results', []) if isinstance(data, dict) else []
    out = []
    for raw in results:
        n = normalize_abs_item(raw)
        if n:
            out.append(n)
    return out

def _get_library_cache():
    """Return (enrich_map, audio_only) for the full Calibre×ABS merge.

    enrich_map   — {str(calibre_id): merged_item} for every Calibre book that
                   matched an ABS edition (mediaTypes includes 'ebook').
    audio_only   — list of merged items that are ABS-only (no Calibre ebook).

    The match runs against ALL Calibre books (not just one page), so the
    consumed set is complete and no ebook-backed audiobook leaks into the
    audio-only list.  Rebuilt at most every _LIBRARY_CACHE_TTL seconds.
    """
    with _library_cache_lock:
        now = time.time()
        if (_library_cache['enrich_map'] is not None
                and now - _library_cache['ts'] < _LIBRARY_CACHE_TTL):
            return _library_cache['enrich_map'], _library_cache['audio_only']

        # Cache miss: fetch everything and run the full merge once.
        all_books, _ = get_calibre_books(limit=0, offset=0)
        abs_items = get_abs_items() if ABS_ENABLED else []

        # Resilience: a transient ABS outage makes get_abs_items() return [],
        # which would otherwise rebuild an audio-stripped cache and pin it for
        # the full TTL — every dual-format work would flip to ebook-only and a
        # matched audiobook could resurface as a separate card. If ABS is
        # enabled, came back empty, and the PREVIOUS build had audio data, keep
        # serving that last-good merge and retry again soon instead of poisoning
        # the cache. (A cold start with ABS down still builds ebook-only — there
        # is nothing better to serve — and self-heals on the next rebuild.)
        prev = _library_cache['enrich_map']
        prev_had_audio = prev is not None and (
            bool(_library_cache['audio_only'])
            or any('audiobook' in (m.get('mediaTypes') or [])
                   for m in prev.values()))
        if ABS_ENABLED and not abs_items and prev_had_audio:
            print("⚠️  ABS returned 0 items — retaining previous library cache "
                  "(transient outage); will retry shortly")
            # Nudge ts so the next call retries in ~15s rather than waiting out
            # the full TTL, but don't hammer ABS on every single request.
            _library_cache['ts'] = now - _LIBRARY_CACHE_TTL + 15
            return _library_cache['enrich_map'], _library_cache['audio_only']

        if abs_items:
            merged = match_works(all_books, abs_items, include_audio_only=True)
        else:
            for b in all_books:
                b.setdefault('mediaTypes', ['ebook'])
            merged = all_books

        enrich_map = {
            str(m['id']): m
            for m in merged
            if 'ebook' in (m.get('mediaTypes') or [])
        }
        audio_only = [
            m for m in merged
            if (m.get('mediaTypes') or []) == ['audiobook']
        ]

        _library_cache.update(enrich_map=enrich_map, audio_only=audio_only, ts=now)
        print(f"Library cache built: {len(enrich_map)} ebook works "
              f"({sum(1 for m in enrich_map.values() if 'audiobook' in (m.get('mediaTypes') or []))} dual-format), "
              f"{len(audio_only)} audio-only")
        return enrich_map, audio_only

# --- matching helpers -------------------------------------------------------

def _norm(s):
    """Normalize a title/string for fuzzy comparison: strip accents, lower,
    drop a leading article, strip punctuation, collapse whitespace."""
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'^(the|a|an)\s+', '', s.lower().strip())
    s = re.sub(r'[^\w\s]', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def _norm_author(name):
    """Normalize an author name so that spaced and unspaced initial formats
    compare equal: 'J. R. R. Tolkien' and 'J.R.R. Tolkien' both → 'jrr tolkien';
    'George R. R. Martin' and 'George R.R. Martin' both → 'george rr martin'."""
    s = _norm(re.sub(r'\.(?=\S)', '', name or ''))
    # Collapse consecutive single-letter words (spaced initials) into one token.
    words = s.split()
    out, buf = [], []
    for w in words:
        if len(w) == 1:
            buf.append(w)
        else:
            if buf:
                out.append(''.join(buf))
                buf = []
            out.append(w)
    if buf:
        out.append(''.join(buf))
    return ' '.join(out)

def _strip_edition(s):
    """Drop edition/format markers that differ between an ebook and its
    audiobook edition so the title keys can match exactly: parenthetical /
    bracketed asides ((Unabridged), (Dramatized Adaptation), (Part 1 of 3),
    [...]) and a leading track number ('03 - ' / '03. '). Author-gated callers
    keep this from producing false positives."""
    s = re.sub(r'\([^)]*\)', ' ', s or '')
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    s = re.sub(r'^\s*\d+\s*[-.]\s+', '', s)
    return s

# A trailing sequence baked into an ABS series name: "Dresden Files #10.4",
# "A Song of Ice and Fire #3", "Dungeon Crawler Carl #8". ABS's minified
# `seriesName` joins the series name and sequence into one string (with no
# separate sequence number), which would otherwise make every book its own
# one-book "series". Captures the numeric (int or decimal) sequence.
_SERIES_SEQ_RE = re.compile(r'\s*#(\d+(?:\.\d+)?)\s*$')

def _split_abs_series(name):
    """Split an ABS series string into (base_name, sequence_or_None). Strips a
    trailing '#N' / '#N.N' so audio-only items group under the same series as
    their Calibre ebook counterparts. Returns (name, None) when no marker."""
    name = (name or '').strip()
    m = _SERIES_SEQ_RE.search(name)
    if not m:
        return name, None
    base = name[:m.start()].strip()
    try:
        seq = float(m.group(1))
    except (TypeError, ValueError):
        seq = None
    return (base or name), seq

def _first_author(item):
    """First author, comma-split so an ABS comma-joined authorName
    ('Robert Jordan, Brandon Sanderson') reduces to the lead author. Calibre's
    display-form names ('Joe Abercrombie') have no comma, so this is a no-op
    there."""
    a = item.get('authors') or []
    name = a[0] if a else (item.get('author') or '')
    return name.split(',')[0].strip()

# A "part" marker inside an ABS title: "(Part 1 of 2)", "(1 of 2)", "Disc 3 of
# 9", or a bare "... 2 of 2". Captures (index, total). We only treat it as a
# real multi-part set when total > 1 (see _group_editions).
_PART_RE = re.compile(r'(?:part|pt\.?|disc|book|vol\.?|cd)?\s*(\d+)\s+of\s+(\d+)', re.I)

def _parse_part(title):
    """(index, total) for a multi-part audiobook title, or (None, None)."""
    m = _PART_RE.search(title or '')
    if not m:
        return None, None
    try:
        return int(m.group(1)), int(m.group(2))
    except (TypeError, ValueError):
        return None, None

def _strip_part(s):
    """Drop a bare 'N of M' part marker (the parenthesised form is already
    removed by _strip_edition; this catches the unwrapped 'Iron Gold 2 of 2')."""
    return _PART_RE.sub(' ', s or '')

def _kind_of(item):
    """Classify an ABS edition: 'dramatized' for GraphicAudio / full-cast /
    dramatized adaptations, else 'audiobook' (a standard narrated reading)."""
    hay = ' '.join(str(item.get(f, '') or '') for f in
                   ('title', 'author', 'publisher')).lower()
    if 'dramatiz' in hay or 'graphic audio' in hay or 'graphicaudio' in hay \
            or 'full cast' in hay or 'full-cast' in hay:
        return 'dramatized'
    return 'audiobook'

def _load_links():
    """Raw manual-overrides dict from disk. {} when the file is absent."""
    if not os.path.exists(LINKS_FILE):
        return {}
    try:
        with _links_lock, open(LINKS_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception as e:
        print(f"⚠️  Could not load links: {e}")
        return {}

def _load_series_overrides():
    """Per-book series overrides from disk, mtime-cached. {} when absent."""
    try:
        mtime = os.stat(SERIES_OVERRIDES_FILE).st_mtime
    except OSError:
        _series_overrides_cache['mtime'], _series_overrides_cache['data'] = None, {}
        return {}
    if _series_overrides_cache['mtime'] != mtime:
        try:
            with _series_overrides_lock, open(SERIES_OVERRIDES_FILE) as f:
                d = json.load(f)
            _series_overrides_cache['data'] = d if isinstance(d, dict) else {}
        except Exception as e:
            print(f"⚠️  Could not load series overrides: {e}")
            _series_overrides_cache['data'] = {}
        _series_overrides_cache['mtime'] = mtime
    return _series_overrides_cache['data']

def _load_universe_overrides():
    """Per-book universe (saga) overrides from disk, mtime-cached. {} when absent."""
    try:
        mtime = os.stat(UNIVERSE_OVERRIDES_FILE).st_mtime
    except OSError:
        _universe_overrides_cache['mtime'], _universe_overrides_cache['data'] = None, {}
        return {}
    if _universe_overrides_cache['mtime'] != mtime:
        try:
            with _universe_overrides_lock, open(UNIVERSE_OVERRIDES_FILE) as f:
                d = json.load(f)
            _universe_overrides_cache['data'] = d if isinstance(d, dict) else {}
        except Exception as e:
            print(f"⚠️  Could not load universe overrides: {e}")
            _universe_overrides_cache['data'] = {}
        _universe_overrides_cache['mtime'] = mtime
    return _universe_overrides_cache['data']

def _apply_series_override(book):
    """Apply any per-book series/universe overrides in place, then return the book.
    A null/None series_index marks the book as 'in the series but unnumbered'
    (sorts ahead of every numbered book and shows no badge).
    Universe overrides assign a book to a saga without touching read-only sources."""
    key = str(book.get('id'))

    overrides = _load_series_overrides()
    if key in overrides:
        ov = overrides[key]
        if isinstance(ov, dict):
            if 'series' in ov:
                book['series'] = ov['series'] or ''
            if 'series_index' in ov:
                book['series_index'] = ov['series_index']
        else:
            book['series_index'] = ov

    universe_overrides = _load_universe_overrides()
    if key in universe_overrides:
        book['universe'] = universe_overrides[key] or ''

    return book

def _normalize_links(raw):
    """Coerce the three accepted on-disk shapes (str / list / {editions:[...]})
    into a uniform { calibre_id(str): [ {kind?, label?, parts:[absId,...]} ] }.
    Drops malformed entries silently."""
    out = {}
    for cid, val in (raw or {}).items():
        specs = []
        if isinstance(val, str):
            if val.strip():
                specs.append({'parts': [val.strip()]})
        elif isinstance(val, list):
            parts = [str(p).strip() for p in val if str(p).strip()]
            if parts:
                specs.append({'parts': parts})
        elif isinstance(val, dict):
            for ed in (val.get('editions') or []):
                if not isinstance(ed, dict):
                    continue
                parts = [str(p).strip() for p in (ed.get('parts') or []) if str(p).strip()]
                if not parts:
                    continue
                specs.append({'kind': ed.get('kind'),
                              'label': ed.get('label'), 'parts': parts})
        if specs:
            out[str(cid)] = specs
    return out

def _make_edition(parts_items, kind=None, label=None):
    """Build a logical audio edition from one-or-more ordered ABS items (parts).
    Sums durations; keeps a private `_item` ref to part 1 for cover/match keys."""
    p0 = parts_items[0]
    k = kind or _kind_of(p0)
    lbl = label or ('Dramatized Audiobook' if k == 'dramatized' else 'Audiobook')
    parts, total_dur = [], 0.0
    for i, a in enumerate(parts_items):
        ab = a.get('audiobook') or {}
        try:
            dur = float(ab.get('duration') or 0)
        except (TypeError, ValueError):
            dur = 0.0
        total_dur += dur
        parts.append({'absId': a['absId'], 'title': a.get('title'),
                      'duration': dur, 'cover': a.get('audioCover') or a.get('cover'),
                      'index': i})
    return {
        'editionId': f'{k}:{p0["absId"]}',
        'kind': k, 'label': lbl, 'parts': parts, 'duration': total_dur,
        'cover': p0.get('audioCover') or p0.get('cover'),
        'narrators': p0.get('narrators', []), 'absId': p0['absId'],
        '_item': p0,
    }

def _group_editions(abs_items, links_norm):
    """Collapse the flat ABS item list into logical editions. Returns
    (editions, forced_assoc) where forced_assoc maps editionId -> calibre_id for
    editions pinned by a manual override. Multi-part sets are auto-detected by
    a shared (base-title, author, kind, part-total) key; everything else is a
    single-part edition. Manual overrides take precedence and are consumed
    first, so they can stitch parts auto-grouping can't (divergent title/author)."""
    by_absid = {a['absId']: a for a in abs_items}
    used, editions, forced_assoc = set(), [], {}

    # 1. Manual overrides first — they can group parts auto-detection misses.
    for cid, specs in links_norm.items():
        for spec in specs:
            items = [by_absid[p] for p in spec['parts']
                     if p in by_absid and p not in used]
            if not items:
                continue
            for a in items:
                used.add(a['absId'])
            ed = _make_edition(items, kind=spec.get('kind'), label=spec.get('label'))
            forced_assoc[ed['editionId']] = str(cid)
            editions.append(ed)

    # 2. Auto-group the rest: real multi-part sets share a key; the part marker
    #    distinguishes them so two standalone same-title editions never merge.
    groups, singles = defaultdict(list), []
    for a in abs_items:
        if a['absId'] in used:
            continue
        raw_t = a.get('_rawTitle', a['title'])
        idx, tot = _parse_part(raw_t)
        if idx and tot and tot > 1:
            base = _norm(_strip_edition(_strip_part(raw_t.split(':')[0])))
            key = (base, _norm_author(_first_author(a)), _kind_of(a), tot)
            groups[key].append((idx, a))
        else:
            singles.append(a)
    for key, lst in groups.items():
        lst.sort(key=lambda t: t[0])
        editions.append(_make_edition([a for _, a in lst]))
    for a in singles:
        editions.append(_make_edition([a]))
    return editions, forced_assoc

def _public_edition(ed):
    """Edition dict minus private (_-prefixed) keys, safe to JSON-serialize."""
    return {k: v for k, v in ed.items() if not k.startswith('_')}

def match_works(calibre_items, abs_items, include_audio_only=True):
    """Merge Calibre + ABS into one unified list. Ebook items keep their Calibre
    id (preserving progress/highlight/cache keys); audio-only items keep
    abs:{absId}. ABS items are first grouped into logical editions (multi-part
    sets stitched), then each Calibre work collects ALL matching editions into
    an `audioEditions` list (a work can have both a standard audiobook and a
    dramatized adaptation). Match order per work: manual link, ISBN, ASIN, then
    title+first-author with subtitle- and edition-stripped retries.

    include_audio_only appends editions that matched nothing; pass False on
    paginated pages>0 so audio-only items aren't repeated on every page."""
    editions, forced_assoc = _group_editions(abs_items, _normalize_links(_load_links()))

    by_isbn, by_asin = defaultdict(list), defaultdict(list)
    by_ta, by_ta_sub, by_ta_strip = defaultdict(list), defaultdict(list), defaultdict(list)
    for ed in editions:
        if ed['editionId'] in forced_assoc:
            continue  # pinned by id; don't also match it by title to other works
        a = ed['_item']
        if a.get('isbn'):
            by_isbn[str(a['isbn']).strip()].append(ed)
        if a.get('asin'):
            by_asin[str(a['asin']).strip()].append(ed)
        na = _norm_author(_first_author(a))
        t = a['title']
        by_ta[(_norm(t), na)].append(ed)
        by_ta_sub[(_norm(t.split(':')[0]), na)].append(ed)
        ks = _norm(_strip_edition(_strip_part(t.split(':')[0])))
        if ks:
            by_ta_strip[(ks, na)].append(ed)

    consumed = set()

    def collect(c):
        found, seen = [], set()
        def add(ed):
            if ed['editionId'] not in consumed and ed['editionId'] not in seen:
                seen.add(ed['editionId'])
                found.append(ed)
        cid = str(c['id'])
        for ed in editions:
            if forced_assoc.get(ed['editionId']) == cid:
                add(ed)
        if c.get('isbn'):
            for ed in by_isbn.get(str(c['isbn']).strip(), []):
                add(ed)
        if c.get('asin'):
            for ed in by_asin.get(str(c['asin']).strip(), []):
                add(ed)
        na = _norm_author(_first_author(c))
        for ed in by_ta.get((_norm(c['title']), na), []):
            add(ed)
        for ed in by_ta_sub.get((_norm(c['title'].split(':')[0]), na), []):
            add(ed)
        ks = _norm(_strip_edition(c['title'].split(':')[0]))
        if ks:
            for ed in by_ta_strip.get((ks, na), []):
                add(ed)
        # Standard audiobook before dramatized; stable otherwise.
        found.sort(key=lambda e: 0 if e['kind'] == 'audiobook' else 1)
        return found

    merged = []
    for c in calibre_items:
        item = dict(c)
        item['mediaTypes'] = ['ebook']
        found = collect(c)
        if found:
            for ed in found:
                consumed.add(ed['editionId'])
            primary = found[0]
            item['mediaTypes'] = ['ebook', 'audiobook']
            item['audioEditions'] = [_public_edition(ed) for ed in found]
            item['absId'] = primary['absId']
            item['audioCover'] = primary.get('cover')
            item['narrators'] = primary.get('narrators', [])
            item['audiobook'] = primary['_item'].get('audiobook')
        merged.append(item)

    if include_audio_only:
        for ed in editions:
            if ed['editionId'] in consumed:
                continue
            row = dict(ed['_item'])
            row['mediaTypes'] = ['audiobook']
            row['audioEditions'] = [_public_edition(ed)]
            merged.append(row)
    return merged

@router.get('/api/ebooks')
def list_books(limit: int | None = None, offset: int = 0, query: str | None = None):
    """List all available books from Calibre"""
    books, total = get_calibre_books(limit=limit, offset=offset, query=query)
    return {
        'books': books,
        'total': total,
        'offset': offset,
        'limit': limit
    }

@router.get('/api/ebooks/{book_id}')
def get_book_info(book_id):
    """Get information about a specific book"""
    book = get_book_metadata(book_id)

    if book:
        return book
    else:
        return JSONResponse({'error': 'Book not found'}, status_code=404)

@router.get('/api/ebooks/{book_id}/cover')
def get_book_cover(book_id, type: str = 'cover'):
    """Proxy book cover from Calibre"""
    cover_type = type  # 'cover' or 'thumb'

    try:
        url = f'{CALIBRE_URL}/get/{cover_type}/{book_id}/{CALIBRE_LIBRARY}'
        # Calibre's default thumb is a useless 60x80; ask for a grid-sized one
        # (scaled to fit the box, preserving aspect). Calibre caches the result
        # on its own disk, so repeat requests are cheap.
        params = {'sz': '400x600'} if cover_type == 'thumb' else None
        response = requests.get(url, params=params, timeout=10)

        if response.status_code == 404:
            # Return a placeholder SVG if no cover
            placeholder = '''<svg width="200" height="300" xmlns="http://www.w3.org/2000/svg">
                <defs>
                    <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
                        <stop offset="0%" style="stop-color:#667eea;stop-opacity:1" />
                        <stop offset="100%" style="stop-color:#764ba2;stop-opacity:1" />
                    </linearGradient>
                </defs>
                <rect width="200" height="300" fill="url(#grad)"/>
                <text x="100" y="150" text-anchor="middle" fill="white" font-size="60">📚</text>
            </svg>'''
            return Response(content=placeholder, media_type='image/svg+xml')

        response.raise_for_status()
        return Response(content=response.content,
                        media_type=response.headers.get('Content-Type', 'image/jpeg'),
                        headers={'Cache-Control': 'public, max-age=2592000'})
    except Exception as e:
        print(f"Error fetching cover: {e}")
        # Return placeholder on error
        placeholder = '<svg width="200" height="300" xmlns="http://www.w3.org/2000/svg"><rect fill="#333"/></svg>'
        return Response(content=placeholder, media_type='image/svg+xml')

@router.get('/api/ebooks/{book_id}/download')
def download_book(book_id, format: str | None = None):
    """Download a book file from Calibre"""
    # Get book metadata to find available formats
    book = get_book_metadata(book_id)

    if not book:
        print(f"❌ Book {book_id} not found")
        return JSONResponse({'error': 'Book not found'}, status_code=404)

    # Get the requested format or use the first available
    fmt = (format if format is not None else (book['formats'][0] if book['formats'] else 'epub')).lower()

    print(f"📚 Download request for book {book_id}: '{book.get('title', 'Unknown')}'")
    print(f"📖 Available formats: {book['formats']}")
    print(f"📥 Requested format: {fmt}")

    if fmt not in [f.lower() for f in book['formats']]:
        print(f"❌ Format {fmt} not available")
        return JSONResponse({'error': f'Format {fmt} not available for this book'}, status_code=404)

    # Proxy the download from Calibre
    try:
        calibre_download_url = f'{CALIBRE_URL}/get/{fmt}/{book_id}/{CALIBRE_LIBRARY}'
        print(f"🌐 Calibre URL: {calibre_download_url}")
        response = requests.get(calibre_download_url, stream=True, timeout=30)
        response.raise_for_status()

        # Create a filename
        safe_title = "".join(c for c in book['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
        filename = f"{safe_title}.{fmt}"

        # Stream the response
        def generate():
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return StreamingResponse(
            generate(),
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Type': response.headers.get('Content-Type', 'application/octet-stream')
            }
        )
    except Exception as e:
        print(f"Error downloading book: {e}")
        return JSONResponse({'error': 'Failed to download book'}, status_code=500)

@router.get('/api/search')
def search_books(q: str = '', limit: int = 50, offset: int = 0):
    """Search books in Calibre library"""
    query = q

    books, total = get_calibre_books(limit=limit, offset=offset, query=query)
    return {
        'books': books,
        'total': total,
        'query': query,
        'offset': offset,
        'limit': limit
    }

# ---------- Audiobooks / unified library (Audiobookshelf) ----------
# These endpoints are additive: /api/books and /api/search above are
# unchanged so the existing ebook UI keeps working byte-for-byte. /api/library
# is the new merged view the audiobook-aware UI will consume; it falls back to
# the Calibre-only list whenever ABS is off or unreachable (never 500s).

@router.get('/api/audiobooks')
def list_audiobooks():
    """Debug/inspection route: the raw normalized ABS audiobook list plus the
    absEnabled flag. Returns absEnabled=false and an empty list when ABS isn't
    configured, so the frontend can feature-detect without guessing."""
    items = get_abs_items() if ABS_ENABLED else []
    return {
        'absEnabled': ABS_ENABLED,
        'audiobooks': items,
        'total': len(items),
    }

@router.get('/api/catalog')
def unified_library(limit: int | None = None, offset: int = 0, query: str | None = None, q: str | None = None):
    """Merged Calibre + ABS library. Ebook items keep their Calibre id (so all
    existing progress/highlight/cache keys still resolve); audio-only items use
    abs:{absId}. Degrades to the exact Calibre-only list when ABS is off or
    down. Audio-only items are appended only on the first page (offset 0) so
    they aren't repeated across paginated requests.

    For regular browsing (no query): matching is pre-computed against the full
    Calibre library (_get_library_cache) so every dual-format work is correctly
    identified regardless of which Calibre page its ebook lives on, and only
    truly unmatched ABS items appear as audio-only.

    For text searches (query param): Calibre paginates the filtered result set
    and we run a one-shot match against that slice (no audio-only appended,
    since text search doesn't cover ABS metadata)."""
    books, total = get_calibre_books(limit=limit, offset=offset, query=query)

    if ABS_ENABLED:
        enrich_map, audio_only = _get_library_cache()
        # Enrich the Calibre slice with its ABS data (dual-format badges/editions)
        # via the pre-built full-library cache — same map used for browse + search.
        merged = [enrich_map.get(str(b['id']), b) for b in books]
        if query:
            # Text search: Calibre's full-text search only covers ebooks, so it
            # never surfaces ABS-only audiobooks. Filter the cached audio-only
            # list by the raw search term (token AND-match on title+author) and
            # append the hits so audiobooks show up in search results too.
            raw = (q or '').strip().lower()
            terms = [t for t in raw.split() if t]
            if terms:
                for m in audio_only:
                    hay = ((m.get('title') or '') + ' '
                           + (m.get('author') or '') + ' '
                           + (m.get('series') or '')).lower()
                    if all(t in hay for t in terms):
                        merged.append(m)
        elif offset == 0:
            # Regular browse: only genuinely unmatched ABS items appear as
            # audio-only, appended once on the first page.
            merged.extend(audio_only)
    else:
        merged = books

    return {
        'books': merged,
        'total': total,
        'offset': offset,
        'limit': limit,
        'absEnabled': ABS_ENABLED,
    }

@router.post('/api/catalog/refresh')
def refresh_library():
    """Force the library caches to rebuild on the next request. Call this after
    importing a book into Calibre so it shows up immediately rather than after
    the (up to 120s) TTL. Cheap and idempotent."""
    _invalidate_library_caches()
    return {'status': 'ok', 'refreshed': True}

@router.get('/api/catalog/{book_id}')
def unified_library_item(book_id):
    """Merged single book: the Calibre work plus any matched ABS editions, in
    the exact shape /api/library rows use. The frontend uses this to (re)load
    in-progress books WITHOUT losing their audiobook side — fetching the
    Calibre-only /api/books/<id> here would strip mediaTypes/absId/audioEditions
    and make a dual-format work look ebook-only. Degrades to the plain Calibre
    metadata when ABS is off. 404 when the Calibre book is gone. Also handles
    audio-only items (id starting with "abs:") when ABS is enabled."""
    book_id_str = str(book_id)

    # Handle audiobook IDs (abs:...)
    if book_id_str.startswith('abs:'):
        if not ABS_ENABLED:
            return JSONResponse({'error': 'Audiobooks not available'}, status_code=404)
        abs_id = book_id_str[4:]

        # First check if this is a dual-format book (has matching Calibre ebook)
        enrich_map, audio_only = _get_library_cache()
        for calibre_id, enriched in enrich_map.items():
            if enriched.get('absId') == abs_id:
                # Found the dual-format book - return it
                return enriched

        # Not dual-format, check audio-only items
        for item in audio_only:
            if item.get('id') == book_id_str or item.get('absId') == abs_id:
                return item

        return JSONResponse({'error': 'Audiobook not found'}, status_code=404)

    # Calibre book
    book = get_book_metadata(book_id)
    if not book:
        return JSONResponse({'error': 'Book not found'}, status_code=404)
    if ABS_ENABLED:
        abs_items = get_abs_items()
        if abs_items:
            merged = match_works([book], abs_items, include_audio_only=False)
            if merged:
                return merged[0]
    book.setdefault('mediaTypes', ['ebook'])
    return book

@router.get('/api/booklinks/{book_id}')
def book_links(book_id):
    """Resolve the cross-format siblings of a book so the reader/player can show
    each other's bookmarks (synced by percentage). Given a Calibre id, returns
    the matched audiobook id(s) as "abs:<absId>"; given an "abs:<absId>", returns
    the Calibre id of the matched ebook. Uses the pre-built library cache so it's
    cheap. Always returns {bookId, siblings:[...]} (empty list when unmatched or
    ABS is off)."""
    book_id = str(book_id)
    siblings = []
    if ABS_ENABLED:
        enrich_map, _ = _get_library_cache()
        if book_id.startswith('abs:'):
            abs_id = book_id[4:]
            for cid, m in enrich_map.items():
                ed_ids = []
                for ed in (m.get('audioEditions') or []):
                    ed_ids += [p.get('absId') for p in (ed.get('parts') or [])]
                if m.get('absId') == abs_id or abs_id in ed_ids:
                    siblings.append(cid)
                    break
        else:
            m = enrich_map.get(book_id)
            if m:
                seen = set()
                for ed in (m.get('audioEditions') or []):
                    for p in (ed.get('parts') or []):
                        aid = p.get('absId')
                        if aid and aid not in seen:
                            seen.add(aid)
                            siblings.append('abs:' + aid)
                if not siblings and m.get('absId'):
                    siblings.append('abs:' + m['absId'])
    return {'bookId': book_id, 'siblings': siblings}

# Fields the series/saga views need per book; drops heavy ones (description,
# audiobook.chapters) so the grouped payload stays lean.
_SERIES_BOOK_FIELDS = ('id', 'title', 'author', 'authors', 'cover', 'thumbnail',
                       'mediaTypes', 'absId', 'audioEditions', 'formats',
                       'format', 'series', 'series_index', 'universe')

def _series_book(b):
    return {k: b.get(k) for k in _SERIES_BOOK_FIELDS if k in b}

def _series_sort_key(book):
    """Order books within a series: unnumbered (series_index is None) first —
    ahead of 0 and negatives — then by numeric index ascending, then title."""
    idx = book.get('series_index')
    numbered = idx is not None
    return (numbered, idx if numbered else 0, book.get('title') or '')

@router.get('/api/series')
def api_series():
    """Group the full merged library into series. Reuses the full-library match
    cache so grouping spans every Calibre page AND ABS audio-only items. Each
    group carries its books (sorted by series_index) so the frontend renders
    both the series grid and a series' detail view from one fetch. Books with
    no series are omitted. Returns {series:[...], absEnabled}."""
    if ABS_ENABLED:
        enrich_map, audio_only = _get_library_cache()
        merged = list(enrich_map.values()) + audio_only
    else:
        merged, _ = get_calibre_books(limit=0, offset=0)
        for b in merged:
            b.setdefault('mediaTypes', ['ebook'])

    groups = {}
    for b in merged:
        name = (b.get('series') or '').strip()
        if not name:
            continue
        g = groups.setdefault(name.lower(), {'name': name, 'books': []})
        g['books'].append(_series_book(b))

    out = []
    for g in groups.values():
        books = sorted(g['books'], key=_series_sort_key)
        mts = set()
        for x in books:
            mts.update(x.get('mediaTypes') or [])
        out.append({
            'name': g['name'],
            'count': len(books),
            'mediaTypes': sorted(mts),
            'cover': books[0].get('cover') or books[0].get('thumbnail'),
            'books': books,
        })
    out.sort(key=lambda s: s['name'].lower())
    return {'series': out, 'absEnabled': ABS_ENABLED}

def _saga_sort_key(book):
    """Order books within a saga: group by series name, then series_index
    (unnumbered first within a series), then title. Books with no series sort
    last (empty series name -> after named ones via the trailing tuple)."""
    series = (book.get('series') or '').strip().lower()
    idx = book.get('series_index')
    numbered = idx is not None
    return (series == '', series, numbered, idx if numbered else 0,
            book.get('title') or '')

@router.get('/api/saga')
def api_saga():
    """Group the library into sagas — the Calibre #universe custom column (e.g.
    "The Cosmere"). Calibre-only: ABS has no equivalent field, so audio-only
    items (which carry no 'universe') are naturally excluded. Books inside a
    saga are sorted by series, then series_index, then title, so a saga reads
    as its constituent series in order. Books with no universe are omitted.
    Returns {sagas:[...], absEnabled}."""
    if ABS_ENABLED:
        enrich_map, audio_only = _get_library_cache()
        merged = list(enrich_map.values()) + audio_only
    else:
        merged, _ = get_calibre_books(limit=0, offset=0)
        for b in merged:
            b.setdefault('mediaTypes', ['ebook'])

    groups = {}
    for b in merged:
        name = (b.get('universe') or '').strip()
        if not name:
            continue
        g = groups.setdefault(name.lower(), {'name': name, 'books': []})
        g['books'].append(_series_book(b))

    out = []
    for g in groups.values():
        books = sorted(g['books'], key=_saga_sort_key)
        mts = set()
        for x in books:
            mts.update(x.get('mediaTypes') or [])
        out.append({
            'name': g['name'],
            'count': len(books),
            'mediaTypes': sorted(mts),
            'cover': books[0].get('cover') or books[0].get('thumbnail'),
            'books': books,
        })
    out.sort(key=lambda s: s['name'].lower())
    return {'sagas': out, 'absEnabled': ABS_ENABLED}

# ---------------------------------------------------------------------------
# Chapter summaries (overlay shown at chapter-end / from reading settings).
# Source sets are parsed from a compendium EPUB/MHT by build_summaries.py into
# backend/summaries/<id>.json. We match a Calibre book to a set's book section
# by normalized title (with an optional manual override file), and return that
# book's ordered chapter summaries. The reader picks the chapter matching the
# user's current position and gates future chapters to avoid spoilers.
# ---------------------------------------------------------------------------

def _summary_norm(s):
    """Normalize a title for matching: lowercase, drop a leading article,
    strip punctuation, collapse whitespace."""
    s = (s or '').strip().lower()
    s = re.sub(r'^(the|a|an)\s+', '', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return s.strip()


def _load_summary_sets():
    """Load + index all backend/summaries/*.json, cached by (file, mtime)
    signature so hand-edits / new sets are picked up without a restart.
    Returns (sets_by_id, book_index) where book_index maps a normalized book
    title to (set_id, book_obj)."""
    try:
        files = sorted(f for f in os.listdir(SUMMARIES_DIR) if f.endswith('.json'))
    except OSError:
        files = []
    sig = tuple((f, os.stat(os.path.join(SUMMARIES_DIR, f)).st_mtime) for f in files)
    with _summaries_lock:
        if _summaries_cache['sig'] == sig:
            return _summaries_cache['sets'], _summaries_cache['book_index']
        sets, book_index = {}, {}
        for f in files:
            try:
                with open(os.path.join(SUMMARIES_DIR, f), encoding='utf-8') as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as e:
                print(f"Skipping summary set {f}: {e}")
                continue
            sid = data.get('id') or f[:-5]
            sets[sid] = data
            for book in data.get('books', []):
                key = _summary_norm(book.get('title'))
                if key and key not in book_index:
                    book_index[key] = (sid, book)
        _summaries_cache.update({'sig': sig, 'sets': sets, 'book_index': book_index})
        return sets, book_index


def _load_summary_links():
    """Optional manual {bookId: {set, book}} overrides. {} when absent."""
    try:
        with open(SUMMARY_LINKS_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


@router.get('/api/summaries/{book_id}')
def get_summaries(book_id):
    """Resolve a book to its chapter-summary set and return the ordered
    chapter summaries for that book. {available: false} when no set matches —
    never an error, so the reader can quietly hide the feature."""
    sets, book_index = _load_summary_sets()
    if not sets:
        return {'available': False, 'reason': 'no summary sets'}

    matched = None       # (set_id, book_obj)
    matched_by = None

    # 1) Manual override by bookId.
    link = _load_summary_links().get(str(book_id))
    if isinstance(link, dict) and link.get('set') in sets:
        for b in sets[link['set']].get('books', []):
            if _summary_norm(b.get('title')) == _summary_norm(link.get('book')):
                matched, matched_by = (link['set'], b), 'override'
                break

    # 2) Match by normalized book title (audio-only "abs:" ids skip Calibre).
    title = ''
    if matched is None and not str(book_id).startswith('abs:'):
        meta = get_book_metadata(book_id)
        if meta:
            title = meta.get('title') or ''
            hit = book_index.get(_summary_norm(title))
            if hit:
                matched, matched_by = hit, 'title'

    if matched is None:
        return {'available': False, 'bookTitle': title}

    set_id, book = matched
    return {
        'available': True,
        'setId': set_id,
        'setTitle': sets[set_id].get('title') or set_id,
        'source': sets[set_id].get('source') or '',
        'bookTitle': book.get('title') or '',
        'matchedBy': matched_by,
        'chapters': book.get('chapters', []),
    }

# ---------------------------------------------------------------------------
# Generic fetch proxy — powers the reader's clean in-app lookup view (wiki /
# dictionary). The reader fetches the MediaWiki action API (ad-free article
# HTML) and dictionaryapi.dev through here so it never hits CORS and we keep a
# single egress point. NOT a general open proxy: http/https only, and private
# / loopback / link-local hosts are refused (basic SSRF guard) since the only
# legitimate targets are public reference sites.
# ---------------------------------------------------------------------------

def _fetch_host_blocked(host):
    """True if `host` is loopback/private/link-local (or unparseable)."""
    import ipaddress
    import socket
    if not host:
        return True
    h = host.split(':')[0].strip('[]').lower()
    if h in ('localhost', '0.0.0.0'):
        return True
    # Resolve to IP(s) and reject any that are private/loopback/link-local.
    try:
        infos = socket.getaddrinfo(h, None)
    except OSError:
        return True
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            return True
    return False


@router.get('/api/fetch')
def fetch_proxy(url: str = ''):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return JSONResponse({'error': 'only http/https'}, status_code=400)
    if _fetch_host_blocked(parsed.hostname):
        return JSONResponse({'error': 'host not allowed'}, status_code=403)
    try:
        upstream = requests.get(url, timeout=12, headers={
            # A real UA — some wikis/CDNs 403 the python-requests default.
            'User-Agent': 'Mozilla/5.0 (GreatReads reader; +ereader) '
                          'AppleWebKit/537.36 Chrome/120 Safari/537.36',
            'Accept': '*/*',
        }, stream=True)
    except requests.RequestException as e:
        return JSONResponse({'error': 'fetch failed', 'detail': str(e)}, status_code=502)
    # Cap body size (reader pages, not downloads) and pass content-type through.
    MAX = 4 * 1024 * 1024
    body = upstream.raw.read(MAX + 1, decode_content=True)
    if len(body) > MAX:
        body = body[:MAX]
    ctype = upstream.headers.get('Content-Type', 'application/octet-stream')
    return Response(content=body, status_code=upstream.status_code,
                    headers={'Content-Type': ctype}, media_type=ctype)

@router.get('/api/audiobooks/{abs_id}/cover')
def get_audiobook_cover(abs_id, type: str = 'cover'):
    """Proxy an Audiobookshelf item cover. Mirrors /api/books/<id>/cover:
    same 30-day cache and SVG placeholder fallback, so the frontend can treat
    audio and ebook covers identically. Returns the placeholder when ABS is
    off, the item has no cover, or anything errors."""
    cover_type = type  # 'cover' or 'thumb'
    placeholder = '''<svg width="200" height="300" xmlns="http://www.w3.org/2000/svg">
        <defs>
            <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#f5576c;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#f093fb;stop-opacity:1" />
            </linearGradient>
        </defs>
        <rect width="200" height="300" fill="url(#grad)"/>
        <text x="100" y="150" text-anchor="middle" fill="white" font-size="60">🎧</text>
    </svg>'''
    if not ABS_ENABLED:
        return Response(content=placeholder, media_type='image/svg+xml')
    try:
        url = f'{ABS_URL}/api/items/{abs_id}/cover'
        # ABS resizes server-side when given a width; ignored (full cover) if
        # unsupported, so this is safe. Keeps the grid payload light.
        params = {'width': 400} if cover_type == 'thumb' else None
        response = requests.get(url, headers=_abs_headers(), params=params, timeout=10)
        if response.status_code == 404:
            return Response(content=placeholder, media_type='image/svg+xml')
        response.raise_for_status()
        return Response(content=response.content,
                        media_type=response.headers.get('Content-Type', 'image/jpeg'),
                        headers={'Cache-Control': 'public, max-age=2592000'})
    except Exception as e:
        print(f"⚠️  Error fetching ABS cover {abs_id}: {e}")
        return Response(content=placeholder, media_type='image/svg+xml')

# ---------- Audiobook playback (ABS session proxy) ----------
# This proxy starts/stops the ABS playback session and hands the client
# the media URLs. HLS tracks (transcode) are routed back through THIS backend's
# /api/audiobooks/hls proxy: ABS sends no CORS headers on /hls, so hls.js's XHR
# from the :8090 WebView would be blocked reading the manifest/segments. Routing
# them through :8091 (permissive CORS) fixes that and keeps the ABS token
# server-side. Direct single-file tracks stay as absolute ?token= URLs — a plain
# <audio> element plays them cross-origin without CORS (no double-hop).

# Default mime set advertised to ABS; presence of these decides DirectPlay vs
# Transcode. Sending [] forces a single stitched HLS manifest.
_ABS_MIME_TYPES = ['audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/ogg', 'audio/flac']

def _rewrite_track_urls(session):
    """Rewrite ABS server-relative contentUrls so the phone can fetch them.
    HLS (/hls/<sid>/output.m3u8) -> the backend HLS proxy (CORS-clean, token
    hidden). Direct files (/s/item/.../Part 1.mp3) -> absolute ?token= ABS URL
    (media element handles cross-origin playback itself)."""
    base = ABS_PUBLIC_URL or ABS_URL
    for t in (session.get('audioTracks') or []):
        p = t.get('contentUrl', '') or ''
        if not p.startswith('/'):
            continue
        if p.startswith('/hls/'):
            # Strip leading /hls/ and any query (token re-added per-request by
            # the proxy). Relative segment names in the manifest then resolve
            # back to the proxy automatically — no body rewriting needed.
            sub = p[len('/hls/'):].split('?', 1)[0]
            t['contentUrl'] = f"http://{PUBLIC_HOST}/api/audiobooks/hls/{sub}"
        else:
            sep = '&' if '?' in p else '?'
            t['contentUrl'] = f"{base}{p}{sep}token={ABS_TOKEN}"
    return session

@router.post('/api/audiobooks/{abs_id}/play')
async def play_audiobook(abs_id, request: Request):
    """Start an ABS playback session for an item and return it with track URLs
    rewritten to absolute token-bearing URLs. Multi-file guard: a DirectPlay
    (playMethod 0) split across >1 file is painful to stitch client-side, so we
    close it and re-request with supportedMimeTypes:[] to force one HLS manifest.
    Read playMethod (0=DirectPlay,1=DirectStream,2=Transcode), currentTime
    (resume point), duration, chapters, and audioTracks on the client."""
    if not ABS_ENABLED:
        return JSONResponse({'error': 'Audiobooks not available'}, status_code=503)
    try:
        client = await request.json()
    except Exception:
        client = None
    client = client or {}
    body = {
        'deviceInfo': {'clientName': 'GreatReads', 'clientVersion': _read_version()},
        'supportedMimeTypes': client.get('supportedMimeTypes', _ABS_MIME_TYPES),
        'mediaPlayer': 'html5',
    }
    session = _abs_post(f'/api/items/{abs_id}/play', body)
    if not session:
        return JSONResponse({'error': 'Could not start playback session'}, status_code=502)

    tracks = session.get('audioTracks') or []
    if session.get('playMethod') == 0 and len(tracks) > 1:
        sid = session.get('id')
        if sid:
            _abs_post(f'/api/session/{sid}/close', {})
        forced = _abs_post(f'/api/items/{abs_id}/play',
                           {**body, 'supportedMimeTypes': []})
        if forced:
            session = forced

    return _rewrite_track_urls(session)

@router.post('/api/audiobooks/sessions/{sid}/sync')
async def sync_audiobook_session(sid, request: Request):
    """Forward a playback sync to ABS (POST /api/session/<sid>/sync). Body:
    {currentTime, timeListened (seconds since the PREVIOUS sync — a delta, not
    cumulative), duration}. Keeps ABS progress + multi-device websocket events
    up to date. Call ~every 15s while playing."""
    if not ABS_ENABLED:
        return JSONResponse({'error': 'Audiobooks not available'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = None
    body = body or {}
    payload = {
        'currentTime': body.get('currentTime', 0),
        'timeListened': body.get('timeListened', 0),
        'duration': body.get('duration', 0),
    }
    res = _abs_post(f'/api/session/{sid}/sync', payload)
    if res is None:
        return JSONResponse({'error': 'sync failed'}, status_code=502)
    return res or {'ok': True}

@router.post('/api/audiobooks/sessions/{sid}/close')
async def close_audiobook_session(sid, request: Request):
    """Close an ABS playback session (POST /api/session/<sid>/close). Optional
    body is forwarded as a final sync. Best-effort: always returns ok so the
    client's beforeunload/sendBeacon path never blocks."""
    if not ABS_ENABLED:
        return JSONResponse({'error': 'Audiobooks not available'}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = None
    body = body or {}
    _abs_post(f'/api/session/{sid}/close', body or {})
    return {'ok': True}

@router.get('/api/audiobooks/hls/{subpath:path}')
def proxy_hls(subpath, request: Request):
    """Proxy ABS HLS manifests + segments through this backend so the WebView
    (served from :8090) can fetch them without CORS errors — ABS sends no
    Access-Control-Allow-Origin on /hls — and without ever seeing the ABS token.
    The manifest's segment names are relative (output-0.ts), so they resolve
    back to this route automatically; no body rewriting needed. The token is
    re-attached server-side on every upstream fetch."""
    if not ABS_ENABLED:
        return Response(status_code=503)
    params = dict(request.query_params)
    params['token'] = ABS_TOKEN
    # ABS transcodes segments on demand, so a freshly-requested .ts (especially
    # right after a seek) 404s until ffmpeg reaches it. Retry briefly before
    # giving up; the manifest itself is always ready immediately.
    is_seg = subpath.endswith('.ts')
    attempts = 12 if is_seg else 1
    r = None
    for i in range(attempts):
        try:
            r = requests.get(f'{ABS_URL}/hls/{subpath}', headers=_abs_headers(),
                             params=params, timeout=30, stream=True)
        except Exception as e:
            print(f"⚠️  ABS HLS proxy {subpath} failed: {e}")
            return Response(status_code=502)
        if r.status_code == 404 and is_seg and i < attempts - 1:
            r.close()
            time.sleep(0.5)
            continue
        break
    if r.status_code >= 400:
        # Pass the upstream status through so hls.js can retry on its side too.
        r.close()
        return Response(status_code=r.status_code)
    ct = r.headers.get('Content-Type', '')
    if subpath.endswith('.m3u8') or 'mpegurl' in ct.lower():
        return Response(content=r.content, media_type='application/vnd.apple.mpegurl',
                        headers={'Cache-Control': 'no-cache'})
    # Stream segment bytes (chunked) so we don't buffer whole .ts files.
    def _gen():
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                yield chunk
    return StreamingResponse(_gen(), media_type=ct or 'video/mp2t',
                             headers={'Cache-Control': 'no-cache'})

# ---------- Highlights & Bookmarks ----------
# Single endpoint family handles both. An item is:
#   { id, type: 'highlight'|'bookmark', bookId, bookTitle, bookAuthor,
#     anchor: int|null, page: int|null, total: int|null,
#     text: str|null, note: str|null, color: str|null, created: epoch_ms }
# Anchor is the data-anchor index from reader.html — it pins the position to
# a specific source-DOM block, surviving font-size / unfold re-pagination.

@router.get('/api/highlights')
def list_highlights(bookId: str | None = None, type: str | None = None, q: str | None = None):
    """List all highlights/bookmarks. Optional filters: bookId, type, q."""
    book_id = bookId
    type_filter = type
    q = (q or '').strip().lower()

    with _highlights_lock:
        items = _load_highlights()

    out = []
    for it in items:
        if book_id and str(it.get('bookId')) != str(book_id):
            continue
        if type_filter and it.get('type') != type_filter:
            continue
        if q:
            hay = ' '.join(str(it.get(f, '') or '') for f in
                           ('text', 'note', 'bookTitle', 'bookAuthor')).lower()
            if q not in hay:
                continue
        out.append(it)
    out.sort(key=lambda x: x.get('created', 0), reverse=True)
    return {'items': out, 'total': len(out)}

@router.post('/api/highlights')
async def create_highlight(request: Request):
    """Create a highlight or bookmark. Body is the partial item; we fill
    in id + created timestamp."""
    try:
        body = await request.json()
    except Exception:
        body = None
    body = body or {}
    if body.get('type') not in ('highlight', 'bookmark', 'auto-bookmark', 'line-bookmark'):
        return JSONResponse({'error': 'type must be "highlight", "bookmark", "auto-bookmark", or "line-bookmark"'}, status_code=400)
    if not body.get('bookId'):
        return JSONResponse({'error': 'bookId is required'}, status_code=400)

    item = {
        'id': str(uuid.uuid4()),
        'type': body['type'],
        'bookId': str(body['bookId']),
        'bookTitle': body.get('bookTitle') or '',
        'bookAuthor': body.get('bookAuthor') or '',
        'anchor': body.get('anchor'),
        # offset = character index into the anchor element's textContent;
        # length = number of characters selected within that single anchor.
        # Together they re-locate the exact span on reload, surviving
        # font-size / pagination changes. Pre-highlight bookmarks omit both.
        #
        # For selections that span MULTIPLE anchors (e.g. across paragraphs),
        # `length` is null and `endAnchor`/`endOffset` carry the end of the
        # range. Single-anchor selections keep `endAnchor`/`endOffset` null
        # for back-compat with older clients.
        'offset': body.get('offset'),
        'length': body.get('length'),
        'endAnchor': body.get('endAnchor'),
        'endOffset': body.get('endOffset'),
        'page': body.get('page'),
        'total': body.get('total'),
        # percent (0..1) is the cross-format coordinate: it lets a bookmark made
        # in the ebook surface in the audiobook player (and vice versa) at the
        # equivalent spot. position/duration (seconds) + mediaType are populated
        # for audiobook-origin bookmarks so the player can seek to the exact
        # second; ebook-origin bookmarks leave them null and rely on percent.
        'percent': body.get('percent'),
        'position': body.get('position'),
        'duration': body.get('duration'),
        'mediaType': body.get('mediaType') or '',
        'text': body.get('text') or '',
        'note': body.get('note') or '',
        'color': body.get('color') or 'yellow',
        # Chapter title at time of creation. Captured client-side from the
        # active TOC entry so the bookmarks list can show "where in the book"
        # without re-resolving against the live tocTree on every render.
        'chapter': body.get('chapter') or '',
        'created': int(time.time() * 1000),
    }
    with _highlights_lock:
        items = _load_highlights()
        items.append(item)
        # Auto-bookmarks: keep only the most recent N per book. Anything older
        # is "stale auto-save data" — the user already has a fresher snapshot.
        if item['type'] == 'auto-bookmark':
            book_id = item['bookId']
            autos = [it for it in items
                     if it.get('type') == 'auto-bookmark'
                     and str(it.get('bookId')) == book_id]
            if len(autos) > AUTO_BOOKMARK_LIMIT_PER_BOOK:
                autos.sort(key=lambda x: x.get('created') or 0, reverse=True)
                keep = {it['id'] for it in autos[:AUTO_BOOKMARK_LIMIT_PER_BOOK]}
                items = [it for it in items
                         if not (it.get('type') == 'auto-bookmark'
                                 and str(it.get('bookId')) == book_id
                                 and it['id'] not in keep)]
        _save_highlights(items)
    return JSONResponse(item, status_code=201)

@router.delete('/api/highlights/{item_id}')
def delete_highlight(item_id):
    with _highlights_lock:
        items = _load_highlights()
        new_items = [it for it in items if it.get('id') != item_id]
        if len(new_items) == len(items):
            return JSONResponse({'error': 'not found'}, status_code=404)
        _save_highlights(new_items)
    return {'deleted': item_id}

@router.put('/api/highlights/{item_id}')
async def update_highlight(item_id, request: Request):
    """Partial update — only allows mutating note/color/text."""
    try:
        body = await request.json()
    except Exception:
        body = None
    body = body or {}
    allowed = ('note', 'color', 'text')
    with _highlights_lock:
        items = _load_highlights()
        for it in items:
            if it.get('id') == item_id:
                for k in allowed:
                    if k in body:
                        it[k] = body[k]
                _save_highlights(items)
                return it
    return JSONResponse({'error': 'not found'}, status_code=404)

# ---------- Reading progress ----------
# Per-book "where am I" record so the same position follows the user across
# devices, app reinstalls (which wipe WebView localStorage), and browsers.
# Item shape:
#   { bookId, bookTitle, bookAuthor, format,
#     anchor: int|null,  page: int|null, total: int|null,
#     progress: 0..1,    fontSize: int|null,
#     updated: epoch_ms }
# Anchor is the data-anchor index from reader.html — pins the exact source-DOM
# block so the position survives font-size / unfold re-pagination.
#
# Audiobook records (keyed by "abs:<absId>") reuse the same store but add
# audio-specific fields so the player can resume to the second and the library
# can show audiobooks in "Continue reading" alongside ebooks:
#   { mediaType: 'audiobook', position: float (seconds), duration: float (seconds),
#     absId: str, progress: 0..1, ... }

@router.get('/api/progress')
def list_progress():
    """List all per-book progress records (used by the library 'continue
    reading' view)."""
    with _progress_lock:
        data = _load_progress()
    items = list(data.values())
    items.sort(key=lambda x: x.get('updated', 0), reverse=True)
    return {'items': items, 'total': len(items)}

@router.get('/api/progress/{book_id}')
def get_progress(book_id):
    with _progress_lock:
        data = _load_progress()
    item = data.get(str(book_id))
    if not item:
        return JSONResponse({'error': 'not found'}, status_code=404)
    return item

@router.put('/api/progress/{book_id}')
async def put_progress(book_id, request: Request):
    """Upsert progress for one book. Body is the partial record; we fill in
    bookId + updated timestamp. Last-writer-wins (the client compares
    `updated` against its local copy before pushing)."""
    try:
        body = await request.json()
    except Exception:
        body = None
    body = body or {}
    # `recentPages` is a rolling buffer of recent valid page-turn samples
    # used to compute WPM / time-remaining in the reader bottom bar. Each
    # entry is {ms: int, words: int}; capped at 10 entries client-side.
    # We accept whatever shape the client sends and just persist it — no
    # validation beyond a list-of-dicts sanity check.
    recent = body.get('recentPages')
    if not isinstance(recent, list):
        recent = []
    item = {
        'bookId':      str(book_id),
        'bookTitle':   body.get('bookTitle') or '',
        'bookAuthor':  body.get('bookAuthor') or '',
        'format':      body.get('format') or '',
        'anchor':      body.get('anchor'),
        'page':        body.get('page'),
        'total':       body.get('total'),
        'progress':    body.get('progress'),
        'fontSize':    body.get('fontSize'),
        'recentPages': recent,
        'updated':     int(time.time() * 1000),
    }
    # Audiobook resume state — only persisted when the client sends it (the
    # audiobook player). Ebook progress PUTs omit these and they stay absent,
    # so this is fully backward compatible.
    if body.get('mediaType'):
        item['mediaType'] = body.get('mediaType')
    if body.get('position') is not None:
        item['position'] = body.get('position')
    if body.get('duration') is not None:
        item['duration'] = body.get('duration')
    if body.get('absId'):
        item['absId'] = body.get('absId')
    # Cross-format resume anchor (#25): the chapter the user is in plus how far
    # through it (0..1). Both formats stamp these on save so the *other* format
    # can resume by matching chapter title instead of a global percent (which
    # drifts because ebook page-density and audio narration pace don't line up).
    # Optional — absent on older clients, which just fall back to percent.
    if body.get('chapterTitle'):
        item['chapterTitle'] = body.get('chapterTitle')
    if body.get('chapterFraction') is not None:
        item['chapterFraction'] = body.get('chapterFraction')
    with _progress_lock:
        data = _load_progress()
        data[str(book_id)] = item
        _save_progress(data)
    # Reflect the % straight into GreatReads (read.current_percent) — no sync job.
    _gr_set_current_percent(book_id, item)
    # Maintain the global cross-book reading-speed baseline (#29): the reader
    # sends its current REAL avg ms-per-word (ebook only) so a freshly-opened
    # book can seed WPM / time-remaining before it has its own samples.
    mpw = body.get('msPerWord')
    if mpw is not None and not str(book_id).startswith('abs:') and body.get('mediaType') != 'audiobook':
        _update_reading_baseline(mpw)
    return item

@router.get('/api/reading-speed')
def get_reading_speed():
    """Global cross-book reading-speed baseline (#29). Seeds the reader's WPM /
    time-remaining on a freshly-opened book before it has its own page-turn
    samples. ms-per-word is layout-invariant, so it transfers across books and
    devices. Returns null fields until the first session has contributed."""
    rec = _kv_get(_READING_SPEED_KEY) or {}
    return {
        'ebook_ms_per_word': rec.get('ebook_ms_per_word'),
        'samples': rec.get('samples') or 0,
        'updated': rec.get('updated') or 0,
    }

@router.delete('/api/progress/{book_id}')
def delete_progress(book_id):
    with _progress_lock:
        data = _load_progress()
        if str(book_id) not in data:
            return JSONResponse({'error': 'not found'}, status_code=404)
        del data[str(book_id)]
        _save_progress(data)
    return {'deleted': str(book_id)}

# ---------- GreatReads progress (now written directly) ----------
# The old title-matching batch "sync" is retired. Reading progress is written
# straight into the GreatReads DB at save time: PUT /api/progress updates the
# ereader_progress table AND read.current_percent for the precisely-resolved
# in-progress reading (see _gr_set_current_percent). The endpoint below is kept
# only so the reader's existing fire-and-forget call doesn't 404.

@router.post('/api/greatreads/sync')
def greatreads_sync():
    """Deprecated no-op. Progress is now written directly to GreatReads at save
    time (PUT /api/progress → read.current_percent), so there is nothing to sync."""
    return {'ok': True, 'deprecated': True,
            'note': 'progress is written directly at save time; no sync needed'}

@router.get('/api/greatreads/format/{book_id}')
def greatreads_get_format(book_id):
    """Get the media format that GreatReads is tracking for this book.

    Returns: {
        media: str | null  # "Physical", "Ebook", "Audio", or null if not found
        readingId: int | null
        status: str | null  # "in_progress", "finished", etc.
    }
    """
    # Get book metadata to find title
    book_meta = None
    if str(book_id).startswith('abs:'):
        # Audiobook - need to fetch from the merged library cache
        if ABS_ENABLED:
            enrich_map, audio_only = _get_library_cache()
            # Check if it's in the matched enrichments
            for cid, enriched in enrich_map.items():
                if enriched.get('absId') == book_id[4:]:
                    book_meta = enriched
                    break
            # If not found, check audio-only items
            if not book_meta:
                for item in audio_only:
                    if item.get('id') == str(book_id):
                        book_meta = item
                        break
    else:
        # Calibre book
        try:
            book_meta = get_book_metadata(int(book_id))
        except (ValueError, TypeError):
            pass

    if not book_meta:
        return {'media': None, 'readingId': None, 'status': None}

    title = book_meta.get('title', '')
    norm_title = _norm(_strip_edition(title))

    # GreatReads' /api/readings/ does NOT support a title filter — only skip,
    # limit, status, media. Pulling the in-progress slice (small N) and matching
    # locally is both correct and cheap; this is the bug that made the old
    # ?book_title= path silently return the first 100 readings.
    try:
        r = requests.get(GREATREADS_URL + '/api/readings/',
                        params={'status': 'in_progress', 'limit': 1000},
                        timeout=15)
        r.raise_for_status()
        readings = r.json()

        for rd in (readings or []):
            rd_title = (rd.get('book') or {}).get('title') or ''
            rd_norm = _norm(_strip_edition(rd_title))
            if rd_norm == norm_title:
                return {
                    'media': rd.get('media'),
                    'readingId': rd.get('id'),
                    'status': rd.get('status'),
                }
    except Exception as e:
        print(f'Warning: Failed to fetch GreatReads format: {e}')

    return {'media': None, 'readingId': None, 'status': None}

@router.post('/api/greatreads/finish')
async def greatreads_finish(request: Request):
    """Mark a book as finished in GreatReads, then surface the next TBR book.

    Body: {
        bookId: str (Calibre id or "abs:<id>"),
        title: str,
        author: str,
        media: str ("Physical" | "Ebook" | "Audio"),
        finishDate: str (YYYY-MM-DD),
        ratings: {  # optional, all 0-10 floats (GreatReads' native scale)
            horror, spice, world_building, writing,
            characters, readability, enjoyment
        },
        readingId: int | null  # optional; skips the title-match fallback
    }

    Returns 200: {success, readingId, message, nextBook: {...}|null}
    Returns 404 when no in-progress GreatReads reading matches this title.
    Returns 502 on any GreatReads API error.
    """
    try:
        body = await request.json()
    except Exception:
        body = None
    body = body or {}
    book_id = str(body.get('bookId', ''))
    title = body.get('title', '')
    author = body.get('author', '')
    media = body.get('media', 'Ebook')
    finish_date = body.get('finishDate', '')
    detailed_ratings = body.get('ratings', {})
    reading_id = body.get('readingId')  # Optional; resolves the exact reading

    if not all([book_id, title, finish_date]):
        return JSONResponse({'error': 'Missing required fields: bookId, title, finishDate'}, status_code=400)

    norm_title = _norm(_strip_edition(title))

    # Resolve the reading we're finishing. Preferred path: the frontend already
    # passed `readingId` from /api/greatreads/format (which now correctly hits
    # ?status=in_progress&limit=1000). Fallback: pull the same in-progress slice
    # and match by normalized title here. We deliberately do NOT fall back to
    # /api/books/?title= or /api/readings/?book_title= — both params are silently
    # ignored upstream and return the first 100 records, which was the original
    # "loopy shit" bug that finished the wrong book.
    try:
        existing = None
        if reading_id:
            try:
                r = requests.get(GREATREADS_URL + f'/api/readings/{reading_id}/', timeout=15)
                r.raise_for_status()
                existing = r.json()
            except Exception as e:
                print(f'Warning: Failed to fetch reading #{reading_id}: {e}')
                reading_id = None  # Force the title fallback below

        if not existing:
            r = requests.get(GREATREADS_URL + '/api/readings/',
                             params={'status': 'in_progress', 'limit': 1000},
                             timeout=15)
            r.raise_for_status()
            for rd in (r.json() or []):
                rd_title = (rd.get('book') or {}).get('title') or ''
                if _norm(_strip_edition(rd_title)) == norm_title:
                    existing = rd
                    break

        if not existing:
            return JSONResponse({
                'error': 'No in-progress GreatReads reading for this book',
                'message': f'Start "{title}" in GreatReads first, then mark it finished here.',
                'nextBook': None,
            }, status_code=404)

        reading_id = existing['id']
        # PUT ratings + finish date in one shot. GreatReads' native UI is a
        # 0-5 integer scale (5 emoji items, parseInt throughout — see
        # ../GreatReads/src/greatreads/static/js/app.js). Values >5 trigger
        # legacy 0-10 backward-compat on read (divides by 2, rounds), so
        # sending raw 0-10 produced inconsistent display: writing=7 rendered
        # as 4 stars, while horror=2 rendered as 2 stars (looked unscaled).
        # Halve + round to int so every rating displays exactly as entered.
        # `rating_overall` stays computed server-side, never sent. The PUT
        # route runs ChainCalculator.recalculate_all_chains() for us.
        update_data = {'date_finished_actual': finish_date}
        for key, value in (detailed_ratings or {}).items():
            try:
                v = max(0, min(5, round(float(value) / 2)))
            except (TypeError, ValueError):
                continue
            update_data[f'rating_{key}'] = v
        ur = requests.put(GREATREADS_URL + f'/api/readings/{reading_id}/',
                          json=update_data, timeout=15)
        ur.raise_for_status()
        message = f'Marked reading #{reading_id} as finished'
    except Exception as e:
        return JSONResponse({'error': 'GreatReads API error', 'detail': str(e)}, status_code=502)

    # Next book in reading order for this media. Use /api/readings/tbr — it's
    # the canonical "what's next" source, already sorted (in-progress first,
    # then not-started by date_est_start). The old id_previous chain walk is
    # broken in practice: most readings in the live DB have id_previous=null,
    # so "find rd where id_previous == reading_id" silently returned nothing.
    # We only LOOK UP the next book here; surfacing it as in-progress is the
    # caller's job (POST /api/greatreads/start-next) so a frontend "Cancel"
    # actually cancels.
    next_book = None
    try:
        tbr_resp = requests.get(GREATREADS_URL + '/api/readings/tbr', timeout=15)
        tbr_resp.raise_for_status()
        for rd in (tbr_resp.json() or []):
            if rd.get('media') != media:
                continue
            if rd.get('id') == reading_id:
                continue  # Skip the one we just finished (in case TBR is stale)
            tbr_title = (rd.get('book') or {}).get('title') or ''
            local_match = None
            if tbr_title:
                norm_tbr = _norm(_strip_edition(tbr_title))
                if ABS_ENABLED:
                    enrich_map, audio_only = _get_library_cache()
                    all_items = list(enrich_map.values()) + audio_only
                else:
                    all_items, _ = get_calibre_books(limit=0, offset=0)
                for item in all_items:
                    if _norm(_strip_edition(item.get('title') or '')) == norm_tbr:
                        local_match = item
                        break
            next_book = {
                'readingId': rd.get('id'),
                'alreadyStarted': bool(rd.get('date_started')),
                'title': tbr_title,
                'media': rd.get('media'),
                'id': (local_match or {}).get('id'),
                'author': (local_match or {}).get('author'),
                'cover': (local_match or {}).get('cover') or (local_match or {}).get('thumbnail'),
                'mediaTypes': (local_match or {}).get('mediaTypes', []),
            }
            break
    except Exception as e:
        print(f'Warning: Failed to find next TBR book for media={media}: {e}')

    # Clear our progress record since book is finished.
    # For dual-format books, we use unified progress (Calibre ID), but we also
    # check for any legacy audiobook-only progress (abs:<absId>) and clear it.
    try:
        with _progress_lock:
            data = _load_progress()
            cleared = []

            # Clear main progress record
            if str(book_id) in data:
                del data[str(book_id)]
                cleared.append(str(book_id))

            # For dual-format books, also clear any abs:<absId> progress
            # (shouldn't exist with unified progress, but defensive)
            if not book_id.startswith('abs:'):
                # Get book metadata to find absId if it exists
                book_meta = None
                if ABS_ENABLED:
                    enrich_map, _ = _get_library_cache()
                    book_meta = enrich_map.get(str(book_id))

                if book_meta and book_meta.get('absId'):
                    abs_key = 'abs:' + book_meta['absId']
                    if abs_key in data:
                        del data[abs_key]
                        cleared.append(abs_key)

            if cleared:
                _save_progress(data)
                print(f'Cleared progress for: {", ".join(cleared)}')
    except Exception as e:
        print(f'Warning: Failed to clear progress: {e}')

    return {
        'success': True,
        'readingId': reading_id,
        'message': message,
        'nextBook': next_book
    }


@router.post('/api/greatreads/start-next')
async def greatreads_start_next(request: Request):
    """Surface a not-yet-started TBR reading as in-progress in GreatReads.

    Split out of /finish so the frontend's Cancel button actually cancels —
    /finish returns the next book's info, the user confirms, then we call
    this. GreatReads' own finish-and-start-next logic only fires when
    id_previous chains are populated; most readings have id_previous=null,
    so we have to start it explicitly via POST /api/readings/{id}/start.

    Body: { readingId: int (required), startDate: "YYYY-MM-DD" (optional;
            defaults to today) }
    Returns 200 {success:true, readingId} on success, 502 on upstream error.
    """
    try:
        body = await request.json()
    except Exception:
        body = None
    body = body or {}
    reading_id = body.get('readingId')
    start_date = body.get('startDate') or ''
    if not reading_id:
        return JSONResponse({'error': 'Missing required field: readingId'}, status_code=400)
    if not start_date:
        from datetime import date
        start_date = date.today().isoformat()
    try:
        r = requests.post(
            GREATREADS_URL + f'/api/readings/{reading_id}/start',
            params={'start_date': start_date}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return JSONResponse({'error': 'GreatReads API error', 'detail': str(e)}, status_code=502)
    return {'success': True, 'readingId': reading_id, 'startDate': start_date}


@router.get('/api/health')
def health_check():
    """Health check endpoint"""
    # Test Calibre connection
    calibre_ok = False
    try:
        response = requests.get(f'{CALIBRE_URL}/ajax/library-info', timeout=5)
        calibre_ok = response.status_code == 200
    except:
        pass

    return {
        'status': 'ok' if calibre_ok else 'degraded',
        'calibre_url': CALIBRE_URL,
        'calibre_library': CALIBRE_LIBRARY,
        'calibre_connected': calibre_ok,
        'version': _read_version(),
    }

@router.get('/api/version')
def get_version():
    """Return the current app version (read live from version.txt so a
    `gvc` bump is reflected without a server restart)."""
    return {'version': _read_version()}

@router.get('/api/build-stamp')
def get_build_stamp():
    """Return a YYMMDD-HH:MM stamp derived from the most recently edited
    web asset (index.html / reader.html). Used by the status-bar build
    pill so users can see at a glance whether the running web code is
    fresh after a server-side edit. Distinct from `/api/version`, which
    tracks the semver of the APK itself."""
    import time as _time
    web_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web')
    candidates = ['index.html', 'reader.html']
    latest = 0.0
    for name in candidates:
        p = os.path.join(web_dir, name)
        try:
            mt = os.path.getmtime(p)
            if mt > latest:
                latest = mt
        except OSError:
            pass
    if latest == 0.0:
        latest = _time.time()
    stamp = _time.strftime('%y%m%d-%H:%M', _time.localtime(latest))
    return {'stamp': stamp}

