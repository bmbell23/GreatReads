"""Bulk + cron metadata backfill (#159).

Fill ONLY the empty metadata fields on a book from Apple Books / Google Books /
OpenLibrary — never clobbering values Calibre or the user already set (the override
policy). Reuses the per-book enrichment engine's provider helpers.

Two entry points:
- ``backfill_ids(db, ids)`` — on-demand, for a bulk selection.
- ``backfill_batch(db, limit)`` — the scheduled sweep; picks the next N books that are
  still missing a synopsis / public rating / genres and enriches them, spending a
  bounded slice of the daily Google quota each run. The "missing fields" query is the
  progress tracker: once a field is filled the book drops out of the queue, so the
  sweep converges without a extra state column.
"""

import logging
import os
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.book import Book
from ..models.inventory import Inventory
from ..models.tag import Tag
from ..discovery.itunes_client import ITunesClient
from .metadata_enrichment_service import (
    _google_fields, _openlibrary_fields, _clean_genre_names, _genre_vocabulary,
    _strip_html, _iso_date,
)

logger = logging.getLogger(__name__)

# Default sweep size per scheduled run (env-overridable). Kept modest so the news poll
# + interactive use still have Google quota headroom.
BATCH_SIZE = int(os.environ.get("METADATA_BACKFILL_BATCH", "10"))


def _needs_fields_filter():
    """A book is a backfill candidate when it's missing a synopsis, a public rating,
    or has no genres — and has a title to look up."""
    return (
        Book.title.isnot(None),
        or_(Book.description.is_(None), Book.public_rating.is_(None), ~Book.tags.any()),
    )


def candidate_query(db: Session):
    conds = _needs_fields_filter()
    return db.query(Book).filter(*conds).order_by(Book.id)


def _get_or_create_tags(db: Session, names) -> list:
    out = []
    for name in names or []:
        name = (name or "").strip()
        if not name:
            continue
        tag = db.query(Tag).filter(Tag.name.ilike(name)).first()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        out.append(tag)
    return out


def _isbn_for(db: Session, book_id: int) -> Optional[str]:
    for inv in db.query(Inventory).filter(Inventory.book_id == book_id).all():
        cand = (inv.isbn_13 or inv.isbn_10 or "").strip()
        if cand:
            return cand
    return None


def backfill_one(db: Session, book: Book, vocab: Optional[dict] = None) -> list:
    """Fill this book's empty fields from the providers. Returns the list of field
    names actually written (empty list = nothing found / nothing was missing).
    Commits on its own so a provider failure mid-batch doesn't lose prior work."""
    title = (book.title or "").strip()
    if not title:
        return []
    author = book.author or ""
    author_last = (book.author_name_second or "").strip()
    isbn = _isbn_for(db, book.id)

    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    g = _google_fields(isbn, title, author, api_key) if api_key else {}
    o = _openlibrary_fields(isbn, title, author, author_last)
    try:
        a = ITunesClient().lookup(title, author) if title else None
    except Exception:
        a = None
    a = a or {}

    if vocab is None:
        vocab = _genre_vocabulary(db)
    applied = []

    # Synopsis — Apple first, then Google.
    if not book.description:
        desc = _strip_html(a.get("description")) or _strip_html(g.get("description"))
        if desc:
            book.description = desc
            applied.append("description")

    # Public rating — Apple or Google (0–5).
    if book.public_rating is None:
        rating = a.get("public_rating")
        if rating is None:
            rating = g.get("public_rating")
        if isinstance(rating, (int, float)):
            book.public_rating = float(rating)
            applied.append("public_rating")

    # Genres — only when the book has none (never remove existing).
    if not book.tags:
        names = []
        for src in (a.get("genres"), g.get("genres")):
            names += _clean_genre_names(src, vocab)
        for src in (o.get("genres"),):
            names += _clean_genre_names(src, vocab, vocab_only=True)
        # dedupe preserving order/canonical casing
        seen, clean = set(), []
        for n in names:
            if n.lower() not in seen:
                seen.add(n.lower())
                clean.append(n)
        if clean:
            book.tags = _get_or_create_tags(db, clean)
            if not book.genre:
                book.genre = clean[0]
            applied.append("genres")

    # Publish date — OpenLibrary (original year) → Apple → Google, ISO only.
    if book.date_published is None:
        from datetime import date as _date
        iso = o.get("date") or _iso_date(None, a.get("release_date")) or g.get("date")
        if iso and len(iso) >= 10:
            try:
                book.date_published = _date.fromisoformat(iso[:10])
                applied.append("date_published")
            except ValueError:
                pass

    # Page count — Google → OpenLibrary.
    if book.page_count is None:
        pc = g.get("page_count") or o.get("page_count")
        if isinstance(pc, int) and pc > 0:
            book.page_count = pc
            applied.append("page_count")

    if applied:
        db.commit()
    return applied


def backfill_ids(db: Session, ids: list) -> dict:
    """On-demand backfill for a specific selection (bulk 'Fetch metadata')."""
    books = db.query(Book).filter(Book.id.in_(ids or [])).all()
    vocab = _genre_vocabulary(db)
    updated, fields_total = 0, 0
    for b in books:
        try:
            applied = backfill_one(db, b, vocab)
        except Exception as exc:
            logger.warning("backfill_one failed for book %s: %s", b.id, exc)
            db.rollback()
            applied = []
        if applied:
            updated += 1
            fields_total += len(applied)
    return {"processed": len(books), "updated": updated, "fields_filled": fields_total}


def backfill_batch(db: Session, limit: int = BATCH_SIZE) -> dict:
    """Scheduled sweep: enrich the next ``limit`` books still missing fields."""
    books = candidate_query(db).limit(limit).all()
    if not books:
        return {"processed": 0, "updated": 0, "fields_filled": 0}
    return backfill_ids(db, [b.id for b in books])
