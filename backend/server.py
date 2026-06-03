#!/usr/bin/env python3
"""
Ereader Backend Server
Serves ebook files from Calibre Content Server via REST API
"""

from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
import os
import json
import uuid
import threading
import time
import re
import unicodedata
from collections import defaultdict

app = Flask(__name__)
CORS(app)  # Enable CORS for mobile app access

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

# Persisted user data (highlights + bookmarks). Single JSON file on disk —
# trivial to back up, trivial to grep. Guarded by a lock because Flask is
# multi-threaded in debug mode.
DATA_DIR = os.environ.get('EREADER_DATA_DIR',
                          os.path.join(os.path.dirname(__file__), 'data'))
os.makedirs(DATA_DIR, exist_ok=True)
HIGHLIGHTS_FILE = os.path.join(DATA_DIR, 'highlights.json')
_highlights_lock = threading.Lock()
# Auto-bookmarks are an "auto-save" of the user's place — we only need the
# most recent N per book. Older ones are pruned on every new auto-bookmark
# create (see /api/highlights POST). Manual / line bookmarks are unbounded.
AUTO_BOOKMARK_LIMIT_PER_BOOK = 5

# Per-book reading progress (last anchor / page / fraction). Same file-on-disk
# pattern as highlights. Shape on disk: { "<bookId>": {progress dict}, ... }
PROGRESS_FILE = os.path.join(DATA_DIR, 'progress.json')
_progress_lock = threading.Lock()

# Feature-request / TODO list surfaced by the in-app "Requests" page. Same
# file-on-disk pattern. Shape on disk: list of request items (see
# /api/requests below for the item shape). Seeded from REQUESTS_SEED on
# first run so a fresh checkout has the initial backlog.
REQUESTS_FILE = os.path.join(DATA_DIR, 'requests.json')
_requests_lock = threading.Lock()
REQUEST_STATUSES = ('Backlog', 'Requested', 'In Progress', 'Done')

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

def _normalize_sections(raw):
    """Coerce a client-supplied `sections` value into the on-disk shape:
    list of {id, title, body}. Drops malformed entries silently rather than
    erroring — the requests UI is single-user and we'd rather not lose a
    long edit to a schema nit. Returns None if input is not a list at all,
    so callers can distinguish "field omitted" from "field cleared to []"."""
    if not isinstance(raw, list):
        return None
    out = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        sid = str(s.get('id') or uuid.uuid4())
        title = str(s.get('title') or 'Untitled').strip() or 'Untitled'
        body = s.get('body') or ''
        if not isinstance(body, str):
            body = str(body)
        out.append({'id': sid, 'title': title, 'body': body})
    return out
REQUESTS_SEED = [
    {'title': 'Adding physical book pages',
     'body':  'Adding physical book pages'},
    {'title': 'Adding reading speed tracking',
     'body':  'Adding reading speed tracking'},
    {'title': 'Making bookmarks be line-specific',
     'body':  'Making bookmarks be line-specific'},
    {'title': "Considering a book's \"end\" to be 100% and not include appendix (100%+)",
     'body':  "Considering a book's \"end\" to be 100% and not include appendix (100%+)"},
]

# Single source of truth for the app version, bumped by `gvc` (see
# ../dotfiles/bashrc/conf.d/20-functions.sh — gvc auto-increments the
# patch number in version.txt, commits, tags, pushes). The frontend
# fetches /api/version on load and the Android build.gradle reads the
# same file at build time, so a single `gvc <msg>` keeps the web pill
# and APK versionName in sync.
VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'version.txt')

def _read_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip() or '0.0.0'
    except OSError:
        return '0.0.0'

def _load_highlights():
    if not os.path.exists(HIGHLIGHTS_FILE):
        return []
    try:
        with open(HIGHLIGHTS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  Could not load highlights: {e}")
        return []

def _save_highlights(items):
    tmp = HIGHLIGHTS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, HIGHLIGHTS_FILE)

def _load_progress():
    if not os.path.exists(PROGRESS_FILE):
        return {}
    try:
        with open(PROGRESS_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"⚠️  Could not load progress: {e}")
        return {}

def _save_progress(data):
    tmp = PROGRESS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PROGRESS_FILE)

def _seed_requests():
    """Materialise REQUESTS_SEED into the requests list. Called only when
    requests.json doesn't exist on disk yet."""
    now = int(time.time() * 1000)
    return [{
        'id':       str(uuid.uuid4()),
        'title':    s['title'],
        'body':     s.get('body', ''),
        'status':   'Backlog',
        'comments': [],
        'created':  now,
        'updated':  now,
    } for s in REQUESTS_SEED]

def _load_requests():
    if not os.path.exists(REQUESTS_FILE):
        seeded = _seed_requests()
        try:
            _save_requests(seeded)
        except Exception as e:
            print(f"⚠️  Could not seed requests: {e}")
        return seeded
    try:
        with open(REQUESTS_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️  Could not load requests: {e}")
        return []

def _save_requests(items):
    tmp = REQUESTS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, REQUESTS_FILE)

def get_calibre_books(limit=None, offset=0, query=None):
    """Fetch books from Calibre Content Server"""
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

        return books, search_data.get('total_num', 0)
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

        return {
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
            'thumbnail': f'http://{host}/api/books/{book_id}/cover?type=thumb',
            'cover': f'http://{host}/api/books/{book_id}/cover',
            'description': book.get('comments', ''),
            'isbn': book.get('isbn', ''),
            'asin': asin or '',
            'published': book.get('pubdate', ''),
            'rating': book.get('rating', 0),
            'wordCount': word_count,
        }
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

        # series: [{name,sequence}] (expanded) or seriesName (minified)
        series, series_index = '', 0
        if isinstance(meta.get('series'), list) and meta['series']:
            s0 = meta['series'][0]
            series = s0.get('name', '') or ''
            try:
                series_index = float(s0.get('sequence') or 0)
            except (TypeError, ValueError):
                series_index = 0
        elif meta.get('seriesName'):
            series = meta['seriesName']

        narrators = meta.get('narrators') or []
        if not narrators and meta.get('narratorName'):
            narrators = [meta['narratorName']]

        return {
            'id': f'abs:{abs_id}',
            'absId': abs_id,
            'title': meta.get('title') or 'Unknown',
            'authors': authors,
            'author': ', '.join(authors),
            'publisher': meta.get('publisher') or '',
            'formats': [],
            'format': 'AUDIO',
            'tags': meta.get('genres', []) or [],
            'series': series,
            'series_index': series_index,
            'thumbnail': f'http://{host}/api/audiobooks/{abs_id}/cover?type=thumb',
            'cover': f'http://{host}/api/audiobooks/{abs_id}/cover',
            'audioCover': f'http://{host}/api/audiobooks/{abs_id}/cover',
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
        }
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

# --- matching helpers -------------------------------------------------------

def _norm(s):
    """Normalize a title/string for fuzzy comparison: strip accents, lower,
    drop a leading article, strip punctuation, collapse whitespace."""
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'^(the|a|an)\s+', '', s.lower().strip())
    s = re.sub(r'[^\w\s]', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def _norm_author(name):
    """Normalize an author name; collapses initials ('J.R.R.' -> 'jrr')."""
    return _norm(re.sub(r'\.(?=\S)', '', name or ''))

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
        idx, tot = _parse_part(a['title'])
        if idx and tot and tot > 1:
            base = _norm(_strip_edition(_strip_part(a['title'].split(':')[0])))
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

@app.route('/api/books', methods=['GET'])
def list_books():
    """List all available books from Calibre"""
    limit = request.args.get('limit', type=int)
    offset = request.args.get('offset', default=0, type=int)
    query = request.args.get('query')

    books, total = get_calibre_books(limit=limit, offset=offset, query=query)
    return jsonify({
        'books': books,
        'total': total,
        'offset': offset,
        'limit': limit
    })

@app.route('/api/books/<book_id>', methods=['GET'])
def get_book_info(book_id):
    """Get information about a specific book"""
    book = get_book_metadata(book_id)

    if book:
        return jsonify(book)
    else:
        return jsonify({'error': 'Book not found'}), 404

@app.route('/api/books/<book_id>/cover', methods=['GET'])
def get_book_cover(book_id):
    """Proxy book cover from Calibre"""
    cover_type = request.args.get('type', 'cover')  # 'cover' or 'thumb'

    try:
        url = f'{CALIBRE_URL}/get/{cover_type}/{book_id}/{CALIBRE_LIBRARY}'
        response = requests.get(url, timeout=10)

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
            return Response(placeholder, mimetype='image/svg+xml')

        response.raise_for_status()
        res = Response(response.content, mimetype=response.headers.get('Content-Type', 'image/jpeg'))
        # Cache covers for 30 days
        res.headers['Cache-Control'] = 'public, max-age=2592000'
        return res
    except Exception as e:
        print(f"Error fetching cover: {e}")
        # Return placeholder on error
        placeholder = '<svg width="200" height="300" xmlns="http://www.w3.org/2000/svg"><rect fill="#333"/></svg>'
        return Response(placeholder, mimetype='image/svg+xml')

@app.route('/api/books/<book_id>/download', methods=['GET'])
def download_book(book_id):
    """Download a book file from Calibre"""
    # Get book metadata to find available formats
    book = get_book_metadata(book_id)

    if not book:
        print(f"❌ Book {book_id} not found")
        return jsonify({'error': 'Book not found'}), 404

    # Get the requested format or use the first available
    fmt = request.args.get('format', book['formats'][0] if book['formats'] else 'epub').lower()

    print(f"📚 Download request for book {book_id}: '{book.get('title', 'Unknown')}'")
    print(f"📖 Available formats: {book['formats']}")
    print(f"📥 Requested format: {fmt}")

    if fmt not in [f.lower() for f in book['formats']]:
        print(f"❌ Format {fmt} not available")
        return jsonify({'error': f'Format {fmt} not available for this book'}), 404

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

        return Response(
            generate(),
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Type': response.headers.get('Content-Type', 'application/octet-stream')
            }
        )
    except Exception as e:
        print(f"Error downloading book: {e}")
        return jsonify({'error': 'Failed to download book'}), 500

@app.route('/api/search', methods=['GET'])
def search_books():
    """Search books in Calibre library"""
    query = request.args.get('q', '')
    limit = request.args.get('limit', default=50, type=int)
    offset = request.args.get('offset', default=0, type=int)

    books, total = get_calibre_books(limit=limit, offset=offset, query=query)
    return jsonify({
        'books': books,
        'total': total,
        'query': query,
        'offset': offset,
        'limit': limit
    })

# ---------- Audiobooks / unified library (Audiobookshelf) ----------
# These endpoints are additive: /api/books and /api/search above are
# unchanged so the existing ebook UI keeps working byte-for-byte. /api/library
# is the new merged view the audiobook-aware UI will consume; it falls back to
# the Calibre-only list whenever ABS is off or unreachable (never 500s).

@app.route('/api/audiobooks', methods=['GET'])
def list_audiobooks():
    """Debug/inspection route: the raw normalized ABS audiobook list plus the
    absEnabled flag. Returns absEnabled=false and an empty list when ABS isn't
    configured, so the frontend can feature-detect without guessing."""
    items = get_abs_items() if ABS_ENABLED else []
    return jsonify({
        'absEnabled': ABS_ENABLED,
        'audiobooks': items,
        'total': len(items),
    })

@app.route('/api/library', methods=['GET'])
def unified_library():
    """Merged Calibre + ABS library. Ebook items keep their Calibre id (so all
    existing progress/highlight/cache keys still resolve); audio-only items use
    abs:{absId}. Degrades to the exact Calibre-only list when ABS is off or
    down. Audio-only items are appended only on the first page (offset 0) so
    they aren't repeated across paginated requests."""
    limit = request.args.get('limit', type=int)
    offset = request.args.get('offset', default=0, type=int)
    query = request.args.get('query')

    books, total = get_calibre_books(limit=limit, offset=offset, query=query)

    abs_items = get_abs_items() if ABS_ENABLED else []
    if abs_items:
        # Only append unmatched audio-only items on the first page; a search
        # query also restricts to the Calibre result set's matches for now.
        include_audio_only = (offset == 0)
        merged = match_works(books, abs_items, include_audio_only=include_audio_only)
    else:
        merged = books

    return jsonify({
        'books': merged,
        'total': total,
        'offset': offset,
        'limit': limit,
        'absEnabled': ABS_ENABLED,
    })

@app.route('/api/library/<book_id>', methods=['GET'])
def unified_library_item(book_id):
    """Merged single book: the Calibre work plus any matched ABS editions, in
    the exact shape /api/library rows use. The frontend uses this to (re)load
    in-progress books WITHOUT losing their audiobook side — fetching the
    Calibre-only /api/books/<id> here would strip mediaTypes/absId/audioEditions
    and make a dual-format work look ebook-only. Degrades to the plain Calibre
    metadata when ABS is off. 404 when the Calibre book is gone."""
    book = get_book_metadata(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    if ABS_ENABLED:
        abs_items = get_abs_items()
        if abs_items:
            merged = match_works([book], abs_items, include_audio_only=False)
            if merged:
                return jsonify(merged[0])
    book.setdefault('mediaTypes', ['ebook'])
    return jsonify(book)

@app.route('/api/audiobooks/<abs_id>/cover', methods=['GET'])
def get_audiobook_cover(abs_id):
    """Proxy an Audiobookshelf item cover. Mirrors /api/books/<id>/cover:
    same 30-day cache and SVG placeholder fallback, so the frontend can treat
    audio and ebook covers identically. Returns the placeholder when ABS is
    off, the item has no cover, or anything errors."""
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
        return Response(placeholder, mimetype='image/svg+xml')
    try:
        url = f'{ABS_URL}/api/items/{abs_id}/cover'
        response = requests.get(url, headers=_abs_headers(), timeout=10)
        if response.status_code == 404:
            return Response(placeholder, mimetype='image/svg+xml')
        response.raise_for_status()
        res = Response(response.content,
                       mimetype=response.headers.get('Content-Type', 'image/jpeg'))
        res.headers['Cache-Control'] = 'public, max-age=2592000'
        return res
    except Exception as e:
        print(f"⚠️  Error fetching ABS cover {abs_id}: {e}")
        return Response(placeholder, mimetype='image/svg+xml')

# ---------- Audiobook playback (ABS session proxy) ----------
# The Flask proxy starts/stops the ABS playback session and hands the client
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

@app.route('/api/audiobooks/<abs_id>/play', methods=['POST'])
def play_audiobook(abs_id):
    """Start an ABS playback session for an item and return it with track URLs
    rewritten to absolute token-bearing URLs. Multi-file guard: a DirectPlay
    (playMethod 0) split across >1 file is painful to stitch client-side, so we
    close it and re-request with supportedMimeTypes:[] to force one HLS manifest.
    Read playMethod (0=DirectPlay,1=DirectStream,2=Transcode), currentTime
    (resume point), duration, chapters, and audioTracks on the client."""
    if not ABS_ENABLED:
        return jsonify({'error': 'Audiobooks not available'}), 503
    client = request.get_json(silent=True) or {}
    body = {
        'deviceInfo': {'clientName': 'GreatReads', 'clientVersion': _read_version()},
        'supportedMimeTypes': client.get('supportedMimeTypes', _ABS_MIME_TYPES),
        'mediaPlayer': 'html5',
    }
    session = _abs_post(f'/api/items/{abs_id}/play', body)
    if not session:
        return jsonify({'error': 'Could not start playback session'}), 502

    tracks = session.get('audioTracks') or []
    if session.get('playMethod') == 0 and len(tracks) > 1:
        sid = session.get('id')
        if sid:
            _abs_post(f'/api/session/{sid}/close', {})
        forced = _abs_post(f'/api/items/{abs_id}/play',
                           {**body, 'supportedMimeTypes': []})
        if forced:
            session = forced

    return jsonify(_rewrite_track_urls(session))

@app.route('/api/audiobooks/sessions/<sid>/sync', methods=['POST'])
def sync_audiobook_session(sid):
    """Forward a playback sync to ABS (POST /api/session/<sid>/sync). Body:
    {currentTime, timeListened (seconds since the PREVIOUS sync — a delta, not
    cumulative), duration}. Keeps ABS progress + multi-device websocket events
    up to date. Call ~every 15s while playing."""
    if not ABS_ENABLED:
        return jsonify({'error': 'Audiobooks not available'}), 503
    body = request.get_json(silent=True) or {}
    payload = {
        'currentTime': body.get('currentTime', 0),
        'timeListened': body.get('timeListened', 0),
        'duration': body.get('duration', 0),
    }
    res = _abs_post(f'/api/session/{sid}/sync', payload)
    if res is None:
        return jsonify({'error': 'sync failed'}), 502
    return jsonify(res or {'ok': True})

@app.route('/api/audiobooks/sessions/<sid>/close', methods=['POST'])
def close_audiobook_session(sid):
    """Close an ABS playback session (POST /api/session/<sid>/close). Optional
    body is forwarded as a final sync. Best-effort: always returns ok so the
    client's beforeunload/sendBeacon path never blocks."""
    if not ABS_ENABLED:
        return jsonify({'error': 'Audiobooks not available'}), 503
    body = request.get_json(silent=True) or {}
    _abs_post(f'/api/session/{sid}/close', body or {})
    return jsonify({'ok': True})

@app.route('/api/audiobooks/hls/<path:subpath>', methods=['GET'])
def proxy_hls(subpath):
    """Proxy ABS HLS manifests + segments through this backend so the WebView
    (served from :8090) can fetch them without CORS errors — ABS sends no
    Access-Control-Allow-Origin on /hls — and without ever seeing the ABS token.
    The manifest's segment names are relative (output-0.ts), so they resolve
    back to this route automatically; no body rewriting needed. The token is
    re-attached server-side on every upstream fetch."""
    if not ABS_ENABLED:
        return Response('', status=503)
    params = dict(request.args)
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
            return Response('', status=502)
        if r.status_code == 404 and is_seg and i < attempts - 1:
            r.close()
            time.sleep(0.5)
            continue
        break
    if r.status_code >= 400:
        # Pass the upstream status through so hls.js can retry on its side too.
        r.close()
        return Response('', status=r.status_code)
    ct = r.headers.get('Content-Type', '')
    if subpath.endswith('.m3u8') or 'mpegurl' in ct.lower():
        res = Response(r.content, mimetype='application/vnd.apple.mpegurl')
        res.headers['Cache-Control'] = 'no-cache'
        return res
    # Stream segment bytes (chunked) so we don't buffer whole .ts files.
    def _gen():
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                yield chunk
    res = Response(_gen(), mimetype=ct or 'video/mp2t')
    res.headers['Cache-Control'] = 'no-cache'
    return res

# ---------- Highlights & Bookmarks ----------
# Single endpoint family handles both. An item is:
#   { id, type: 'highlight'|'bookmark', bookId, bookTitle, bookAuthor,
#     anchor: int|null, page: int|null, total: int|null,
#     text: str|null, note: str|null, color: str|null, created: epoch_ms }
# Anchor is the data-anchor index from reader.html — it pins the position to
# a specific source-DOM block, surviving font-size / unfold re-pagination.

@app.route('/api/highlights', methods=['GET'])
def list_highlights():
    """List all highlights/bookmarks. Optional filters: bookId, type, q."""
    book_id = request.args.get('bookId')
    type_filter = request.args.get('type')
    q = (request.args.get('q') or '').strip().lower()

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
    return jsonify({'items': out, 'total': len(out)})

@app.route('/api/highlights', methods=['POST'])
def create_highlight():
    """Create a highlight or bookmark. Body is the partial item; we fill
    in id + created timestamp."""
    body = request.get_json(silent=True) or {}
    if body.get('type') not in ('highlight', 'bookmark', 'auto-bookmark', 'line-bookmark'):
        return jsonify({'error': 'type must be "highlight", "bookmark", "auto-bookmark", or "line-bookmark"'}), 400
    if not body.get('bookId'):
        return jsonify({'error': 'bookId is required'}), 400

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
    return jsonify(item), 201

@app.route('/api/highlights/<item_id>', methods=['DELETE'])
def delete_highlight(item_id):
    with _highlights_lock:
        items = _load_highlights()
        new_items = [it for it in items if it.get('id') != item_id]
        if len(new_items) == len(items):
            return jsonify({'error': 'not found'}), 404
        _save_highlights(new_items)
    return jsonify({'deleted': item_id})

@app.route('/api/highlights/<item_id>', methods=['PUT'])
def update_highlight(item_id):
    """Partial update — only allows mutating note/color/text."""
    body = request.get_json(silent=True) or {}
    allowed = ('note', 'color', 'text')
    with _highlights_lock:
        items = _load_highlights()
        for it in items:
            if it.get('id') == item_id:
                for k in allowed:
                    if k in body:
                        it[k] = body[k]
                _save_highlights(items)
                return jsonify(it)
    return jsonify({'error': 'not found'}), 404

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

@app.route('/api/progress', methods=['GET'])
def list_progress():
    """List all per-book progress records (used by the library 'continue
    reading' view)."""
    with _progress_lock:
        data = _load_progress()
    items = list(data.values())
    items.sort(key=lambda x: x.get('updated', 0), reverse=True)
    return jsonify({'items': items, 'total': len(items)})

@app.route('/api/progress/<book_id>', methods=['GET'])
def get_progress(book_id):
    with _progress_lock:
        data = _load_progress()
    item = data.get(str(book_id))
    if not item:
        return jsonify({'error': 'not found'}), 404
    return jsonify(item)

@app.route('/api/progress/<book_id>', methods=['PUT'])
def put_progress(book_id):
    """Upsert progress for one book. Body is the partial record; we fill in
    bookId + updated timestamp. Last-writer-wins (the client compares
    `updated` against its local copy before pushing)."""
    body = request.get_json(silent=True) or {}
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
    with _progress_lock:
        data = _load_progress()
        data[str(book_id)] = item
        _save_progress(data)
    return jsonify(item)

@app.route('/api/progress/<book_id>', methods=['DELETE'])
def delete_progress(book_id):
    with _progress_lock:
        data = _load_progress()
        if str(book_id) not in data:
            return jsonify({'error': 'not found'}), 404
        del data[str(book_id)]
        _save_progress(data)
    return jsonify({'deleted': str(book_id)})

# ---------- Feature requests / TODO list ----------
# Backing store for the in-app "Requests" page (web/requests.html). One JSON
# file on disk, same atomic-write pattern as highlights/progress. Item shape:
#   { id, title, body, status, comments: [{ts, author, text}],
#     created: epoch_ms, updated: epoch_ms }
# status is one of REQUEST_STATUSES. "Requested" is the bucket that the next
# Augment/agent session should pick up (see .augment-guidelines).

@app.route('/api/requests', methods=['GET'])
def list_requests():
    """List all requests, newest-updated first. Optional ?status= filter."""
    status_filter = request.args.get('status')
    with _requests_lock:
        items = _load_requests()
    if status_filter:
        items = [it for it in items if it.get('status') == status_filter]
    items.sort(key=lambda x: x.get('updated', 0), reverse=True)
    return jsonify({'items': items, 'total': len(items)})

@app.route('/api/requests', methods=['POST'])
def create_request():
    """Create a new request. Defaults to Backlog if status is omitted.

    Optional `sections` field — list of {id?, title, body} where body is
    HTML produced by the rich-text editor. When present it supersedes the
    legacy single-string `body`; both are persisted for back-compat so
    older clients keep rendering something."""
    body = request.get_json(silent=True) or {}
    title = (body.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'title is required'}), 400
    status = body.get('status') or 'Backlog'
    if status not in REQUEST_STATUSES:
        return jsonify({'error': f'status must be one of {REQUEST_STATUSES}'}), 400
    now = int(time.time() * 1000)
    sections = _normalize_sections(body.get('sections'))
    item = {
        'id':       str(uuid.uuid4()),
        'title':    title,
        'body':     body.get('body') or '',
        'sections': sections if sections is not None else [],
        'status':   status,
        'comments': [],
        'created':  now,
        'updated':  now,
    }
    with _requests_lock:
        items = _load_requests()
        items.append(item)
        _save_requests(items)
    return jsonify(item), 201

@app.route('/api/requests/<item_id>', methods=['PUT'])
def update_request(item_id):
    """Partial update — allows mutating title, body, status, sections."""
    body = request.get_json(silent=True) or {}
    allowed = ('title', 'body', 'status')
    if 'status' in body and body['status'] not in REQUEST_STATUSES:
        return jsonify({'error': f'status must be one of {REQUEST_STATUSES}'}), 400
    sections = None
    if 'sections' in body:
        sections = _normalize_sections(body.get('sections'))
        if sections is None:
            return jsonify({'error': 'sections must be a list'}), 400
    with _requests_lock:
        items = _load_requests()
        for it in items:
            if it.get('id') == item_id:
                for k in allowed:
                    if k in body:
                        it[k] = body[k]
                if sections is not None:
                    it['sections'] = sections
                it['updated'] = int(time.time() * 1000)
                _save_requests(items)
                return jsonify(it)
    return jsonify({'error': 'not found'}), 404

@app.route('/api/requests/<item_id>', methods=['DELETE'])
def delete_request(item_id):
    with _requests_lock:
        items = _load_requests()
        new_items = [it for it in items if it.get('id') != item_id]
        if len(new_items) == len(items):
            return jsonify({'error': 'not found'}), 404
        _save_requests(new_items)
    return jsonify({'deleted': item_id})

@app.route('/api/requests/<item_id>/comments', methods=['POST'])
def add_request_comment(item_id):
    """Append a comment to a request (used for iteration / replies).
    Body: { text: str, author?: str }. Author defaults to 'user'."""
    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text is required'}), 400
    comment = {
        'ts':     int(time.time() * 1000),
        'author': body.get('author') or 'user',
        'text':   text,
    }
    with _requests_lock:
        items = _load_requests()
        for it in items:
            if it.get('id') == item_id:
                it.setdefault('comments', []).append(comment)
                it['updated'] = comment['ts']
                _save_requests(items)
                return jsonify(it), 201
    return jsonify({'error': 'not found'}), 404

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    # Test Calibre connection
    calibre_ok = False
    try:
        response = requests.get(f'{CALIBRE_URL}/ajax/library-info', timeout=5)
        calibre_ok = response.status_code == 200
    except:
        pass

    return jsonify({
        'status': 'ok' if calibre_ok else 'degraded',
        'calibre_url': CALIBRE_URL,
        'calibre_library': CALIBRE_LIBRARY,
        'calibre_connected': calibre_ok,
        'version': _read_version(),
    })

@app.route('/api/version', methods=['GET'])
def get_version():
    """Return the current app version (read live from version.txt so a
    `gvc` bump is reflected without a server restart)."""
    return jsonify({'version': _read_version()})

@app.route('/api/build-stamp', methods=['GET'])
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
    return jsonify({'stamp': stamp})

if __name__ == '__main__':
    print(f"Ereader Backend Server")
    print(f"======================")
    print(f"Calibre URL: {CALIBRE_URL}")
    print(f"Calibre Library: {CALIBRE_LIBRARY}")

    # Test Calibre connection
    try:
        response = requests.get(f'{CALIBRE_URL}/ajax/library-info', timeout=5)
        if response.status_code == 200:
            print(f"✓ Connected to Calibre Content Server")
            libraries = response.json().get('library_map', {})
            print(f"  Available libraries: {', '.join(libraries.keys())}")
        else:
            print(f"✗ Could not connect to Calibre Content Server")
    except Exception as e:
        print(f"✗ Error connecting to Calibre: {e}")
        print(f"  Make sure Calibre Content Server is running at {CALIBRE_URL}")

    print(f"\nStarting server on http://0.0.0.0:8091")
    # Run server - accessible from local network
    app.run(host='0.0.0.0', port=8091, debug=True)
