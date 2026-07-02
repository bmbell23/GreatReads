"""Per-book metadata enrichment (#119).

Query OpenLibrary + Google Books for ONE known book, merge the best value per
field, cross-reference the two sources, and return per-field suggestion rows for
the Edit Book "Request metadata" compare window.

Thin v1 — no house-style normalization (that's #69). **Read-only:** this module
never writes. The client applies accepted values via the existing
``PUT /api/books/{id}`` and ``POST /api/books/{id}/cover/from-url`` endpoints.

Provider strategy (per #119): no fixed primary. Each source is queried at most
once, then per field we take the best value — OpenLibrary for the *original*
publish year and covers, Google for genre; either for pages/series #/cover — and
**cross-reference**: when both agree the row is badged "confirmed by both"; when
they disagree both candidates are offered so the user picks. Results are cached
briefly to protect Google's daily quota against repeat opens.
"""

import os
import re
import time
from typing import Optional

from sqlalchemy.orm import Session

from ..models.book import Book
from ..models.inventory import Inventory
from ..models.tag import Tag
from ..discovery.google_books_client import GoogleBooksClient
from ..discovery.openlibrary_client import OpenLibraryClient
from ..discovery.itunes_client import ITunesClient

# book_id -> (fetched_at, payload). Re-opening the compare window within the TTL
# reuses the fetch instead of spending another Google quota unit.
_CACHE: dict[int, tuple[float, dict]] = {}
_CACHE_TTL = 6 * 3600  # 6 hours

_OL = "OpenLibrary"
_GB = "Google Books"
_AP = "Apple Books"


def _first(seq, pred):
    for x in seq:
        if pred(x):
            return x
    return None


def _iso_date(year: Optional[int], full: Optional[str] = None) -> Optional[str]:
    """Normalize a provider date to an ISO ``YYYY-MM-DD`` string for the Date
    column. Prefer a full date when the source has one; else ``year-01-01``."""
    if full and len(full) >= 10 and full[4] == "-" and full[7] == "-":
        return full[:10]
    if isinstance(year, int) and year > 0:
        return f"{year:04d}-01-01"
    return None


def _date_display(iso: Optional[str]) -> Optional[str]:
    """Show a year-only value (``YYYY-01-01``, our fill for a bare year) as the
    bare year; show a real full date as-is."""
    if not iso:
        return None
    return iso[:4] if iso.endswith("-01-01") else iso


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: Optional[str]) -> Optional[str]:
    """Apple/Google synopses arrive as HTML. Flatten to plain text for storage."""
    if not text:
        return None
    text = _TAG_RE.sub("", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text or None


# Genres arrive noisy: Google "Fiction / Fantasy / Epic", OL "Fiction, fantasy",
# Apple "Sci-Fi & Fantasy". Split on separators, drop noise, keep clean single
# genre names so the vocabulary stays convergent (#158 — user wants clean genres).
_GENRE_SPLIT = re.compile(r"\s*[/,;>]\s*|\s+&\s+|\s+--\s+")
_GENRE_DROP = {"general", "fiction", "nonfiction", "non-fiction", "books"}


def _clean_genre_names(raw_list, vocab: dict, vocab_only: bool = False) -> list:
    """Normalize provider genre strings into clean, deduped names, snapping to the
    library's existing casing when a name already exists (avoids 16 variants of one
    genre). ``vocab`` maps lowercased name -> canonical stored name.

    ``vocab_only=True`` (used for OpenLibrary, whose ``subject`` list is noisy LoC
    headings like "Manuscripts" / "Kings and rulers") keeps ONLY names the library
    already uses as a genre — so OL can corroborate existing genres but never
    introduces junk into the vocabulary."""
    out: list = []
    seen: set = set()
    for raw in raw_list or []:
        if not isinstance(raw, str):
            continue
        for part in _GENRE_SPLIT.split(raw):
            name = part.strip().strip(".").strip()
            low = name.lower()
            if not name or low in _GENRE_DROP or low in seen:
                continue
            if len(name) > 32 or any(ch.isdigit() for ch in name):
                continue  # OL noise like "American fiction 20th century"
            if vocab_only and low not in vocab:
                continue
            seen.add(low)
            out.append(vocab.get(low, name))
    return out


def _google_fields(isbn: Optional[str], title: str, author: str, api_key: str) -> dict:
    """One Google Books lookup (ISBN if present, else title+author), merged across
    the returned editions into the best value per field."""
    gb = GoogleBooksClient(api_key=api_key)
    editions: list[dict] = []
    try:
        if isbn:
            one = gb.get_book_by_isbn(isbn)
            editions = [one] if one else []
        if not editions and title:
            editions = gb.get_editions(title, author)
    except Exception:
        editions = []
    if not editions:
        return {}

    years = [e["year"] for e in editions if isinstance(e.get("year"), int)]
    pages = _first(editions, lambda e: e.get("page_count"))
    cats = _first(editions, lambda e: e.get("categories"))
    snum = _first(editions, lambda e: e.get("series_number") is not None)
    thumb = _first(editions, lambda e: e.get("thumbnail"))
    ref = _first(editions, lambda e: e.get("google_books_id"))
    desc = _first(editions, lambda e: e.get("description"))
    rating = _first(editions, lambda e: isinstance(e.get("average_rating"), (int, float)))

    # A single-edition ISBN hit can carry a full publish date; a title+author
    # sweep spans reprints, so take the earliest year (closest to original).
    if isbn and len(editions) == 1:
        e0 = editions[0]
        date = _iso_date(e0.get("year"), e0.get("published_date"))
    else:
        date = _iso_date(min(years) if years else None)

    return {
        "date": date,
        "page_count": pages.get("page_count") if pages else None,
        "genres": cats.get("categories") if cats else None,   # list (#158)
        "series_number": snum.get("series_number") if snum else None,
        "cover_url": thumb.get("thumbnail") if thumb else None,
        "ref": ref.get("google_books_id") if ref else None,
        "description": _strip_html(desc.get("description")) if desc else None,
        "public_rating": rating.get("average_rating") if rating else None,
    }


def _openlibrary_fields(isbn: Optional[str], title: str, author: str, author_last: str) -> dict:
    """OpenLibrary: the work's original publish year (always, keyless), plus
    pages + cover from the edition when an ISBN is available.

    Search by the author's *last name* rather than the full stored name: this
    library stores initials packed ("JRR", "JK"), which OpenLibrary's author
    match misses, whereas a last-name query ("Tolkien") reliably hits. (Proper
    house-style author normalization is #69; this is just a better query key.)"""
    olc = OpenLibraryClient()
    year = None
    subjects: list = []
    edition: dict = {}
    try:
        if title:
            year = olc.first_publish_year(title, author_last or author)
            subjects = olc.subjects(title, author_last or author)
        if isbn:
            edition = olc.edition_by_isbn(isbn) or {}
    except Exception:
        pass
    return {
        "date": _iso_date(year),
        "page_count": edition.get("pages"),
        "cover_url": edition.get("cover_url"),
        "genres": subjects,
    }


def _build_field(field: str, label: str, current, raw_candidates, is_cover=False,
                 kind: str = "scalar") -> Optional[dict]:
    """Dedupe candidates by their apply-value; when the same value comes from more
    than one source, collapse to one row badged as agreed ("confirmed by both")."""
    merged: list[dict] = []
    for c in raw_candidates:
        if not c:
            continue
        key = c.get("url") if is_cover else c.get("value")
        if key in (None, ""):
            continue
        existing = _first(
            merged, lambda m: (m.get("url") if is_cover else m.get("value")) == key
        )
        if existing:
            if c["source"] not in existing["sources"]:
                existing["sources"].append(c["source"])
                existing["agree"] = True
        else:
            merged.append({**c, "sources": [c["source"]], "agree": False})
    if not merged:
        return None
    for m in merged:
        m["source"] = " + ".join(m.pop("sources"))
    return {
        "field": field,
        "label": label,
        "current": current,
        "is_cover": is_cover,
        "kind": kind,
        "candidates": merged,
    }


def _build_genres(current_names: list, sourced) -> Optional[dict]:
    """Multi-select Genres row (#158). ``sourced`` = list of (names, source). One
    candidate per distinct genre name (case-insensitive), badged with its source(s);
    genres already on the book are pre-selected. Apply unions selected into the book."""
    cur_lower = {n.lower() for n in current_names}
    merged: list[dict] = []
    index: dict[str, dict] = {}
    for names, source in sourced:
        for name in names or []:
            low = name.lower()
            existing = index.get(low)
            if existing:
                if source not in existing["sources"]:
                    existing["sources"].append(source)
                continue
            row = {"value": name, "sources": [source], "on_book": low in cur_lower}
            index[low] = row
            merged.append(row)
    if not merged:
        return None
    for m in merged:
        m["source"] = " + ".join(m.pop("sources"))
    return {
        "field": "genres",
        "label": "Genres",
        "kind": "genres",
        "current": current_names,
        "candidates": merged,
    }


def _genre_vocabulary(db: Session) -> dict:
    """lowercased genre name -> canonical stored name, drawn from existing Genres
    (Tag names) + legacy single-genre strings, so provider names snap to what the
    library already uses instead of spawning near-duplicates."""
    vocab: dict[str, str] = {}
    for (name,) in db.query(Tag.name).all():
        if name:
            vocab.setdefault(name.lower(), name)
    for (g,) in db.query(Book.genre).filter(Book.genre.isnot(None)).distinct().all():
        if g:
            vocab.setdefault(g.lower(), g)
    return vocab


def _rating_display(v) -> Optional[str]:
    if not isinstance(v, (int, float)):
        return None
    return f"★ {float(v):.1f} / 5"


def suggest_metadata(db: Session, book_id: int) -> Optional[dict]:
    """Compare-window payload for one saved book, or None if it doesn't exist."""
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        return None
    # Prefer an ISBN from inventory as the precise lookup key; most records
    # (and all Unowned ones) have none → fall back to title+author.
    isbn = None
    for inv in db.query(Inventory).filter(Inventory.book_id == book_id).all():
        cand = (inv.isbn_13 or inv.isbn_10 or "").strip()
        if cand:
            isbn = cand
            break
    current = {
        "date": book.date_published.isoformat() if book.date_published else None,
        "page_count": book.page_count,
        "series_number": book.series_number,
        "genres": [t.name for t in book.tags] if book.tags else [],
        "public_rating": book.public_rating,
        "description": book.description,
        "cover": bool(book.cover),
    }
    return _suggest_core(db, book_id=book_id, title=(book.title or "").strip(),
                         author=book.author or "", author_last=(book.author_name_second or "").strip(),
                         isbn=isbn, current=current)


def suggest_metadata_adhoc(db: Session, title: str, author: str) -> Optional[dict]:
    """Compare-window payload for a NOT-yet-saved book (a release / new entry, #161):
    look up by title+author with no id and no current values. The client applies accepted
    candidates into the Add-book form (there's nothing to PUT yet)."""
    title = (title or "").strip()
    if not title:
        return None
    author = (author or "").strip()
    author_last = author.rsplit(" ", 1)[-1] if author else ""
    empty = {"date": None, "page_count": None, "series_number": None,
             "genres": [], "public_rating": None, "description": None, "cover": False}
    return _suggest_core(db, book_id=None, title=title, author=author,
                         author_last=author_last, isbn=None, current=empty)


def _suggest_core(db: Session, *, book_id, title, author, author_last, isbn, current) -> Optional[dict]:
    """Shared engine for the compare window — used by both the saved-book path and the
    id-less adhoc path (#161). ``current`` carries the book's present values (all empty
    for a new book) so each row can show 'Keep current'."""
    cache_key = book_id if book_id is not None else f"adhoc:{title}|{author}".lower()
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    g = _google_fields(isbn, title, author, api_key) if api_key else {}
    o = _openlibrary_fields(isbn, title, author, author_last)
    # Apple Books — free/keyless full record: cover (usually the highest-res, #130),
    # synopsis, primary genre and community rating (#158).
    try:
        a = ITunesClient().lookup(title, author) if title else None
    except Exception:
        a = None
    a = a or {}
    apple_desc = _strip_html(a.get("description"))

    def cand(value, source, display=None, url=None):
        if value in (None, "") and not url:
            return None
        return {"value": value, "display": (display if display is not None else value),
                "source": source, "url": url}

    def trunc(text, n=160):
        text = text or ""
        return text if len(text) <= n else text[:n].rstrip() + "…"

    vocab = _genre_vocabulary(db)
    current_genres = list(current.get("genres") or [])
    genre_candidates = [
        (_clean_genre_names(a.get("genres"), vocab), _AP),
        (_clean_genre_names(g.get("genres"), vocab), _GB),
        # OL subjects are noisy LoC headings → corroborate known genres only.
        (_clean_genre_names(o.get("genres"), vocab, vocab_only=True), _OL),
    ]

    cur_date = current.get("date")
    cur_syn = _strip_html(current.get("description"))
    fields = [
        _build_field(
            "date_published", "Published", _date_display(cur_date),
            [cand(o.get("date"), _OL, _date_display(o.get("date"))),
             cand(_iso_date(None, a.get("release_date")), _AP,
                  _date_display(_iso_date(None, a.get("release_date")))),
             cand(g.get("date"), _GB, _date_display(g.get("date")))],
        ),
        _build_field(
            "page_count", "Pages", current.get("page_count"),
            [cand(g.get("page_count"), _GB),
             cand(o.get("page_count"), _OL)],
        ),
        _build_field(
            "series_number", "Series #", current.get("series_number"),
            [cand(g.get("series_number"), _GB)],
        ),
        _build_genres(current_genres, genre_candidates),
        _build_field(
            "public_rating", "Public rating", _rating_display(current.get("public_rating")),
            [cand(a.get("public_rating"), _AP, _rating_display(a.get("public_rating"))),
             cand(g.get("public_rating"), _GB, _rating_display(g.get("public_rating")))],
        ),
        _build_field(
            "description", "Synopsis", trunc(cur_syn),
            [cand(apple_desc, _AP, trunc(apple_desc)),
             cand(g.get("description"), _GB, trunc(g.get("description")))],
            kind="text",
        ),
        _build_field(
            "cover", "Cover", bool(current.get("cover")),
            # Google Books covers are dropped — they're frequently the 575×750 "image
            # not available" placeholder. Apple Books first, OpenLibrary fallback.
            [cand(None, _AP, "Apple Books cover", url=a.get("cover_url")),
             cand(None, _OL, "OpenLibrary cover", url=o.get("cover_url"))],
            is_cover=True,
        ),
    ]

    payload = {
        "book_id": book_id,
        "query": {"mode": "isbn" if isbn else "title_author",
                  "isbn": isbn, "title": title, "author": author},
        "providers": {"google": bool(api_key and g), "openlibrary": bool(o),
                      "apple": bool(a)},
        "fields": [f for f in fields if f],
    }
    _CACHE[cache_key] = (time.time(), payload)
    return payload
