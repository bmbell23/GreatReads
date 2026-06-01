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

app = Flask(__name__)
CORS(app)  # Enable CORS for mobile app access

# Calibre Content Server configuration
CALIBRE_URL = os.environ.get('CALIBRE_URL', 'http://localhost:8083')
CALIBRE_LIBRARY = os.environ.get('CALIBRE_LIBRARY', 'library')

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
            'published': book.get('pubdate', ''),
            'rating': book.get('rating', 0),
            'wordCount': word_count,
        }
    except Exception as e:
        print(f"Error fetching book {book_id}: {e}")
        return None

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
