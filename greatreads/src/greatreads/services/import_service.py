"""
Import service for Calibre and Audiobookshelf libraries.

Both external DBs are opened READ-ONLY (uri=True + ?mode=ro).
We never write to them.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models.book import Book
from ..models.inventory import Inventory
from ..models.external_import import ExternalImport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_author(full_name: str) -> tuple[Optional[str], Optional[str]]:
    """Split 'First Last' into (first, last).  Handles middle initials."""
    parts = full_name.strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return " ".join(parts[:-1]), parts[-1]


def _copy_cover(src: Path, book_id: int) -> bool:
    """Copy a cover image into GreatReads covers directory. Returns True on success."""
    if not src.exists():
        return False
    dest_dir = settings.covers_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{book_id}.jpg"
    try:
        shutil.copy2(src, dest)
        return True
    except Exception as exc:
        logger.warning("Could not copy cover %s → %s: %s", src, dest, exc)
        return False


def _ro_connect(db_path: str) -> Optional[sqlite3.Connection]:
    """Open a SQLite DB read-only via URI.  Returns None if file missing."""
    p = Path(db_path)
    if not p.exists():
        logger.warning("External DB not found: %s", db_path)
        return None
    uri = f"file:{p.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _already_imported(db: Session, source: str, external_id: str) -> bool:
    return (
        db.query(ExternalImport)
        .filter_by(source=source, external_id=external_id)
        .first()
        is not None
    )


def _word_tokens(s: str) -> frozenset:
    """Lowercase, normalize '&'→'and', strip punctuation, return frozenset of words.
    The &↔and normalization lets 'Mother of Death & Dawn' match '… and Dawn' so imports
    link instead of spawning a duplicate (#148)."""
    return frozenset(re.sub(r"[^\w\s]", "", s.lower().replace("&", " and ")).split())


def _get_calibre_universe_map(conn: sqlite3.Connection) -> dict[int, str]:
    """Return {book_id: universe_name} from Calibre's custom Universe column."""
    try:
        row = conn.execute(
            "SELECT id FROM custom_columns WHERE label = 'universe' LIMIT 1"
        ).fetchone()
        if not row:
            return {}
        col_id = row[0]
        table = f"custom_column_{col_id}"
        rows = conn.execute(f"SELECT book, value FROM {table}").fetchall()
        return {r[0]: r[1] for r in rows if r[1]}
    except Exception as exc:
        logger.warning("Could not read Calibre universe column: %s", exc)
        return {}


def _get_calibre_universe(conn: sqlite3.Connection, book_id: int) -> Optional[str]:
    """Return the Universe custom field value for a single Calibre book."""
    try:
        row = conn.execute(
            "SELECT id FROM custom_columns WHERE label = 'universe' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        col_id = row[0]
        table = f"custom_column_{col_id}"
        uni_row = conn.execute(
            f"SELECT value FROM {table} WHERE book = ? LIMIT 1", (book_id,)
        ).fetchone()
        return uni_row[0] if uni_row else None
    except Exception as exc:
        logger.warning("Could not read Calibre universe for book %s: %s", book_id, exc)
        return None


def _get_calibre_word_count(conn: sqlite3.Connection, book_id: int) -> Optional[int]:
    """Return the word_count custom field value for a single Calibre book."""
    try:
        row = conn.execute(
            "SELECT id FROM custom_columns WHERE label = 'word_count' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        table = f"custom_column_{row[0]}"
        wc_row = conn.execute(
            f"SELECT value FROM {table} WHERE book = ? LIMIT 1", (book_id,)
        ).fetchone()
        return int(wc_row[0]) if wc_row and wc_row[0] else None
    except Exception as exc:
        logger.warning("Could not read Calibre word_count for book %s: %s", book_id, exc)
        return None


def _get_calibre_word_count_map(conn: sqlite3.Connection) -> dict[int, int]:
    """Return {calibre_book_id: word_count} for all books that have one."""
    try:
        row = conn.execute(
            "SELECT id FROM custom_columns WHERE label = 'word_count' LIMIT 1"
        ).fetchone()
        if not row:
            return {}
        table = f"custom_column_{row[0]}"
        rows = conn.execute(f"SELECT book, value FROM {table}").fetchall()
        return {r[0]: int(r[1]) for r in rows if r[1]}
    except Exception as exc:
        logger.warning("Could not read Calibre word_count map: %s", exc)
        return {}


def _best_match(
    title: str,
    author: str,
    series: Optional[str],
    series_number: Optional[float],
    year: Optional[str],
    existing: list[tuple],
) -> Optional[dict]:
    """Multi-factor similarity match against existing GreatReads books.

    existing items: (book_id, title, author, title_tokens, series, series_number,
                     year, author_tokens)

    Scoring:
      1.0  = certain (title, author, series, series_number, and year all match)
      0.85+ = likely duplicate (high title similarity with minor differences)
      0.65+ = similar (shown in UI, not flagged for dupe review)
      < 0.65 = not returned
    """
    in_title_tok = _word_tokens(title)
    in_author_tok = _word_tokens(author)
    in_series_norm = (series or "").strip().lower()
    in_year = (year or "")[:4]

    if not in_title_tok:
        return None

    best_score = 0.0
    best_book = None

    for book_id, book_title, book_author, book_title_tok, book_series, book_series_num, book_year, book_author_tok, book_universe in existing:
        if not book_title_tok:
            continue

        # --- Title similarity (Jaccard) — primary gate ---
        t_union = len(in_title_tok | book_title_tok)
        title_sim = len(in_title_tok & book_title_tok) / t_union if t_union else 0.0
        if title_sim < 0.5:
            continue  # titles are too different

        # --- Author similarity ---
        if in_author_tok and book_author_tok:
            a_union = len(in_author_tok | book_author_tok)
            author_sim = len(in_author_tok & book_author_tok) / a_union if a_union else 0.0
        else:
            author_sim = 0.5  # one side missing → neutral

        # --- Series match ---
        book_series_norm = (book_series or "").strip().lower()
        if in_series_norm and book_series_norm:
            series_sim = 1.0 if in_series_norm == book_series_norm else 0.0
        elif not in_series_norm and not book_series_norm:
            series_sim = 1.0  # both have no series
        else:
            series_sim = 0.5  # one has series, one doesn't

        # --- Series number match ---
        if series_number is not None and book_series_num is not None:
            snum_sim = 1.0 if series_number == book_series_num else 0.0
        elif series_number is None and book_series_num is None:
            snum_sim = 1.0
        else:
            snum_sim = 0.5  # one has number, one doesn't

        # --- Year match ---
        book_year_str = (book_year or "")[:4]
        if in_year and book_year_str:
            try:
                year_diff = abs(int(in_year) - int(book_year_str))
                year_sim = 1.0 if year_diff == 0 else (0.8 if year_diff <= 1 else 0.3)
            except ValueError:
                year_sim = 0.5
        else:
            year_sim = 0.7  # one or both years unknown

        # --- Certain match: every factor aligns perfectly ---
        if (title_sim >= 1.0 and author_sim >= 0.8 and
                series_sim == 1.0 and snum_sim == 1.0 and year_sim >= 0.8):
            composite = 1.0
        else:
            # Weighted composite: title 50%, author 25%, series 10%, snum 5%, year 10%
            composite = (
                title_sim  * 0.50 +
                author_sim * 0.25 +
                series_sim * 0.10 +
                snum_sim   * 0.05 +
                year_sim   * 0.10
            )
            # Strong identity (#135): a (near-)exact title AND author is the same
            # work even when the incoming source (e.g. a Calibre ebook) carries no
            # series/number/year to corroborate — absent fields shouldn't sink an
            # otherwise-exact match under the auto-link bar (title+author alone = 0.75,
            # and "one side missing" series/snum/year drag it to ~0.895 < 0.90, so an
            # obvious duplicate spawns a new book instead of linking). Floor the score
            # so it auto-links. Distinct titles (series siblings) score far below 0.92,
            # so this can't mis-link different works.
            if title_sim >= 0.92 and author_sim >= 0.9:
                composite = max(composite, 0.95)

        if composite > best_score:
            best_score = composite
            best_book = (book_id, book_title, book_author, book_series, book_series_num, book_year, book_universe)

    if best_score >= 0.65 and best_book:
        return {
            "id": best_book[0],
            "title": best_book[1],
            "author": best_book[2],
            "series": best_book[3],
            "series_number": best_book[4],
            "year": best_book[5],
            "universe": best_book[6],
            "score": round(best_score, 2),
        }
    return None


def _coerce_date(value: Any) -> Optional[date]:
    """Convert a string or date-like value to a Python date, or None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value[:10]).date()
        except (ValueError, TypeError):
            return None
    return None


def get_calibre_cover_path(cal_id: str) -> Optional[Path]:
    """Return path to a Calibre book's cover.jpg, or None if unavailable."""
    conn = _ro_connect(settings.calibre_db_path)
    if conn is None:
        return None
    row = conn.execute(
        f"SELECT path, has_cover FROM books WHERE id = {int(cal_id)}"
    ).fetchone()
    conn.close()
    if not row or not row[1]:
        return None
    p = Path(settings.calibre_library_path) / row[0] / "cover.jpg"
    return p if p.exists() else None


def get_abs_cover_path(abs_id: str) -> Optional[Path]:
    """Return path to an ABS item's cover.jpg, or None if unavailable."""
    p = Path(settings.abs_metadata_path) / "items" / abs_id / "cover.jpg"
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Calibre
# ---------------------------------------------------------------------------

CALIBRE_QUERY = """
SELECT
    b.id,
    b.title,
    b.pubdate,
    b.has_cover,
    b.path,
    b.series_index,
    s.name  AS series,
    a.name  AS author,
    r.rating,
    c.text  AS description,
    GROUP_CONCAT(CASE WHEN i.type='isbn' THEN i.val END) AS isbn
FROM books b
LEFT JOIN books_authors_link  bal ON b.id = bal.book
LEFT JOIN authors              a   ON bal.author = a.id
LEFT JOIN books_series_link   bsl ON b.id = bsl.book
LEFT JOIN series              s   ON bsl.series = s.id
LEFT JOIN books_ratings_link  brl ON b.id = brl.book
LEFT JOIN ratings             r   ON brl.rating = r.id
LEFT JOIN comments            c   ON b.id = c.book
LEFT JOIN identifiers         i   ON b.id = i.book
GROUP BY b.id
ORDER BY b.title COLLATE NOCASE
"""


def _build_existing(db: Session) -> list[tuple]:
    """Pre-tokenize GreatReads books for similarity matching.

    Each tuple: (book_id, title, author_str, title_tokens, series,
                 series_number, year_str, author_tokens, universe)
    """
    books = db.query(
        Book.id, Book.title, Book.author_name_first, Book.author_name_second,
        Book.series, Book.series_number, Book.date_published, Book.universe,
    ).all()
    result = []
    for b in books:
        author_str = " ".join(filter(None, [b.author_name_first, b.author_name_second]))
        result.append((
            b.id,
            b.title,
            author_str,
            _word_tokens(b.title or ""),
            b.series,
            b.series_number,
            str(b.date_published.year) if b.date_published else None,
            _word_tokens(author_str),
            b.universe,
        ))
    return result


def get_calibre_candidates(db: Session) -> list[dict[str, Any]]:
    """Return Calibre books that have NOT yet been imported."""
    conn = _ro_connect(settings.calibre_db_path)
    if conn is None:
        return []

    already = {
        r.external_id
        for r in db.query(ExternalImport).filter_by(source="calibre").all()
    }

    existing = _build_existing(db)
    universe_map = _get_calibre_universe_map(conn)

    rows = conn.execute(CALIBRE_QUERY).fetchall()
    conn.close()

    results = []
    for row in rows:
        cal_id = str(row[0])
        if cal_id in already:
            continue
        first, last = _split_author(row[7] or "")
        cal_series_num = float(row[5]) if row[5] is not None else None
        # Ignore Calibre's 0101-01-01 "undefined date" sentinel.
        cal_date = row[2][:10] if (row[2] and not row[2].startswith("0101-01-01")) else None
        cal_year = cal_date[:4] if cal_date else None
        candidate = {
            "external_id": cal_id,
            "title": row[1],
            "author": row[7],
            "author_name_first": first,
            "author_name_second": last,
            "date_published": cal_date,
            "has_cover": bool(row[3]),
            "calibre_path": row[4],
            "series": row[6],
            "series_number": cal_series_num,
            "universe": universe_map.get(int(cal_id)),
            "description": row[9],
            "isbn": row[10],
            "similar_to": _best_match(
                row[1] or "", row[7] or "", row[6], cal_series_num, cal_year, existing
            ),
        }
        results.append(candidate)
    return results


def refresh_calibre_metadata(db: Session) -> dict[str, Any]:
    """Backfill EMPTY GreatReads fields from Calibre for already-linked books (#147).

    GreatReads snapshots Calibre metadata once at import and never refreshes, so a book
    auto-imported with sparse epub metadata (before the user completed it in Calibre)
    stays sparse. This re-reads Calibre for every calibre-linked book and fills ONLY
    fields that are currently blank — date_published, word_count, series, series_number,
    universe — never clobbering values the user may have edited (the override layer).
    """
    conn = _ro_connect(settings.calibre_db_path)
    if conn is None:
        return {"updated": 0, "book_ids": []}

    rows = {str(r[0]): r for r in conn.execute(CALIBRE_QUERY).fetchall()}
    links = db.query(ExternalImport).filter_by(source="calibre").all()

    updated = 0
    changed: list[int] = []
    for link in links:
        row = rows.get(str(link.external_id))
        if row is None:
            continue
        book = db.query(Book).filter_by(id=link.book_id).first()
        if book is None:
            continue

        cal_id = int(link.external_id)
        pub_date = None
        if row[2] and not str(row[2]).startswith("0101-01-01"):
            try:
                _d = datetime.fromisoformat(row[2][:10]).date()
                if _d.year > 101:
                    pub_date = _d
            except ValueError:
                pass

        fills: dict[str, Any] = {}
        # A missing date OR a previously-imported 0101 sentinel (year <= 101) is fixable:
        # fill the real Calibre date, or clear the junk sentinel if Calibre has none.
        date_bad = (book.date_published is None) or (book.date_published.year <= 101)
        if date_bad and pub_date:
            fills["date_published"] = pub_date
        elif date_bad and book.date_published is not None:
            fills["date_published"] = None   # drop the 0101 junk
        if not book.word_count:
            wc = _get_calibre_word_count(conn, cal_id)
            if wc:
                fills["word_count"] = wc
        if not book.series and row[6]:
            fills["series"] = row[6]
        if book.series_number is None and row[5] is not None:
            fills["series_number"] = float(row[5])
        if not book.universe:
            uni = _get_calibre_universe(conn, cal_id)
            if uni:
                fills["universe"] = uni

        if fills:
            for k, v in fills.items():
                setattr(book, k, v)
            updated += 1
            changed.append(book.id)

    conn.close()
    if updated:
        db.commit()
    return {"updated": updated, "book_ids": changed}


def import_calibre_book(
    db: Session,
    cal_id: str,
    *,
    book_id: Optional[int] = None,          # if linking to existing
    override: Optional[dict] = None,        # field overrides for new book
) -> dict[str, Any]:
    """
    Import (or link) a single Calibre book into GreatReads.

    - If book_id is None  → create a new Book (+ Inventory with owned_ebook=True)
    - If book_id is given → link the Calibre record to that existing Book
    Returns the resulting GreatReads book dict.
    """
    # Fetch the single record from Calibre
    conn = _ro_connect(settings.calibre_db_path)
    if conn is None:
        raise RuntimeError("Calibre DB not accessible")

    row = conn.execute(
        CALIBRE_QUERY.replace("ORDER BY b.title COLLATE NOCASE", f"HAVING b.id = {int(cal_id)}")
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"Calibre book {cal_id} not found")

    # Read custom fields before closing connection
    universe = _get_calibre_universe(conn, int(cal_id))
    word_count = _get_calibre_word_count(conn, int(cal_id))
    conn.close()

    first, last = _split_author(row[7] or "")
    isbn_raw = row[10] or ""
    isbn_13 = isbn_raw[:13] if len(isbn_raw) >= 13 else None
    isbn_10 = isbn_raw[:10] if (len(isbn_raw) == 10 or (isbn_13 is None and len(isbn_raw) >= 10)) else None

    pub_date = None
    if row[2]:
        try:
            _d = datetime.fromisoformat(row[2][:10]).date()
            # Calibre stores 0101-01-01 as its "undefined date" sentinel — not a real
            # publication date, so don't import it (it renders as "Jan 1, 101").
            if _d.year > 101:
                pub_date = _d
        except ValueError:
            pass

    cal_series = row[6]
    cal_series_num = float(row[5]) if row[5] is not None else None

    if book_id is None:
        # Build field dict, apply any overrides
        fields = {
            "title": row[1],
            "author_name_first": first,
            "author_name_second": last,
            "date_published": pub_date,
            "universe": universe,
            "series": cal_series,
            "series_number": cal_series_num,
            "word_count": word_count,
            "cover": False,
        }
        if override:
            fields.update(override)

        # Ensure date_published is a proper Python date object (override may send strings)
        fields["date_published"] = _coerce_date(fields.get("date_published"))

        book = Book(**fields)
        db.add(book)
        db.flush()  # get book.id

        # Copy cover
        if row[3] and row[4]:
            cover_src = Path(settings.calibre_library_path) / row[4] / "cover.jpg"
            if _copy_cover(cover_src, book.id):
                book.cover = True

        # Create inventory entry (ebook)
        inv = Inventory(
            book_id=book.id,
            owned_ebook=True,
            isbn_13=isbn_13,
            isbn_10=isbn_10,
        )
        db.add(inv)
        action = "created"
    else:
        book = db.query(Book).get(book_id)
        if book is None:
            raise ValueError(f"GreatReads book {book_id} not found")
        # Copy cover if not already present
        if not book.cover and row[3] and row[4]:
            cover_src = Path(settings.calibre_library_path) / row[4] / "cover.jpg"
            if _copy_cover(cover_src, book.id):
                book.cover = True
        # Merge series: if Calibre has one and the existing book doesn't, bring it in
        if cal_series and not book.series:
            book.series = cal_series
            book.series_number = cal_series_num
        # Merge universe: same logic
        if universe and not book.universe:
            book.universe = universe
        # Merge word count: fill in if missing
        if word_count and not book.word_count:
            book.word_count = word_count
        # Apply explicit user-selected field overrides (take precedence over auto-merge)
        if override:
            for field in ('series', 'series_number', 'universe'):
                if field in override:
                    setattr(book, field, override[field])
            if 'date_published' in override:
                book.date_published = _coerce_date(override['date_published'])
        # Mark ebook ownership on the existing inventory row (create one if absent)
        inv = db.query(Inventory).filter_by(book_id=book.id).first()
        if inv is None:
            inv = Inventory(book_id=book.id, owned_ebook=True,
                            isbn_13=isbn_13, isbn_10=isbn_10)
            db.add(inv)
        else:
            inv.owned_ebook = True
            if isbn_13 and not inv.isbn_13:
                inv.isbn_13 = isbn_13
            if isbn_10 and not inv.isbn_10:
                inv.isbn_10 = isbn_10
        action = "linked"

    record = ExternalImport(
        source="calibre",
        external_id=cal_id,
        book_id=book.id,
        action=action,
        imported_at=datetime.utcnow(),
    )
    db.add(record)
    db.commit()
    db.refresh(book)
    return book.to_dict()


def backfill_calibre_word_counts(db: Session) -> dict[str, int]:
    """Backfill word_count for all Calibre-imported GreatReads books missing it.

    Returns a summary dict: {"updated": N, "skipped": M, "no_calibre_data": K}
    """
    conn = _ro_connect(settings.calibre_db_path)
    if conn is None:
        raise RuntimeError("Calibre DB not accessible")

    wc_map = _get_calibre_word_count_map(conn)
    conn.close()

    if not wc_map:
        return {"updated": 0, "skipped": 0, "no_calibre_data": 0}

    # Find all Calibre-linked GreatReads books missing a word count
    records = (
        db.query(ExternalImport)
        .filter(ExternalImport.source == "calibre")
        .all()
    )

    updated = skipped = no_calibre_data = 0
    seen_book_ids: set[int] = set()

    for rec in records:
        if rec.book_id in seen_book_ids:
            continue
        seen_book_ids.add(rec.book_id)

        book = db.query(Book).get(rec.book_id)
        if book is None or book.word_count:
            skipped += 1
            continue

        try:
            cal_id = int(rec.external_id)
        except (TypeError, ValueError):
            skipped += 1
            continue

        wc = wc_map.get(cal_id)
        if wc:
            book.word_count = wc
            updated += 1
        else:
            no_calibre_data += 1

    db.commit()
    return {"updated": updated, "skipped": skipped, "no_calibre_data": no_calibre_data}


def backfill_abs_word_counts(db: Session) -> dict:
    """Estimate and back-fill word counts for ABS-imported books that have none.

    Uses the ABS DB's ``b.duration`` column (seconds) together with the
    ``_estimate_word_count`` helper (_ABS_WPM = 150) to compute an estimate
    and stores it on the GreatReads Book row.
    """
    conn = _ro_connect(settings.abs_db_path)
    if conn is None:
        raise RuntimeError("ABS DB not accessible")

    # Build a map: abs_item_id → duration_seconds
    dur_rows = conn.execute(
        "SELECT li.id, b.duration FROM libraryItems li"
        " JOIN books b ON li.mediaId = b.id"
        " WHERE li.mediaType = 'book'"
    ).fetchall()
    conn.close()
    dur_map: dict[str, float] = {r[0]: r[1] for r in dur_rows if r[1]}

    # Find all ABS-linked GreatReads books missing a word count
    records = (
        db.query(ExternalImport)
        .filter(ExternalImport.source == "audiobookshelf")
        .all()
    )

    updated = skipped = no_abs_data = 0
    seen_book_ids: set[int] = set()

    for rec in records:
        if rec.book_id in seen_book_ids:
            continue
        seen_book_ids.add(rec.book_id)

        book = db.query(Book).get(rec.book_id)
        if book is None or book.word_count:
            skipped += 1
            continue

        # The external_id may be "id1||id2||id3" for multi-part books —
        # sum durations for all parts so the estimate covers the full book.
        part_ids = rec.external_id.split("||")
        total_seconds = sum(dur_map.get(pid, 0.0) for pid in part_ids) or None

        # Also pull other import records for the same book (part IDs stored separately)
        if not total_seconds:
            other_recs = (
                db.query(ExternalImport)
                .filter(
                    ExternalImport.source == "audiobookshelf",
                    ExternalImport.book_id == rec.book_id,
                )
                .all()
            )
            all_ids = {pid for r in other_recs for pid in r.external_id.split("||")}
            total_seconds = sum(dur_map.get(pid, 0.0) for pid in all_ids) or None

        wc = _estimate_word_count(total_seconds)
        if wc:
            book.word_count = wc
            updated += 1
        else:
            no_abs_data += 1

    db.commit()
    return {"updated": updated, "skipped": skipped, "no_abs_data": no_abs_data}


# ---------------------------------------------------------------------------
# Audiobookshelf
# ---------------------------------------------------------------------------

ABS_QUERY = """
SELECT
    li.id            AS item_id,
    li.title,
    li.authorNamesFirstLast,
    b.publishedYear,
    b.isbn,
    b.asin,
    b.coverPath,
    b.duration,
    b.genres,
    s.name           AS series,
    bs.sequence      AS series_seq,
    b.publisher,
    b.narrators
FROM libraryItems li
JOIN books b ON li.mediaId = b.id
LEFT JOIN bookAuthors  ba ON ba.bookId = b.id
LEFT JOIN bookSeries   bs ON bs.bookId = b.id
LEFT JOIN series        s  ON bs.seriesId = s.id
WHERE li.mediaType = 'book'
ORDER BY li.title COLLATE NOCASE
"""




def _is_graphic_audio(title: str, publisher: Optional[str], narrators: Optional[str]) -> bool:
    """Return True when an ABS item is a Graphic Audio dramatized adaptation.

    Detection signals (case-insensitive):
    * Title contains 'Dramatized Adaptation' or 'Graphic Audio'
    * Publisher contains 'Graphic Audio' / 'GraphicAudio'
    * Narrators JSON contains 'GraphicAudio' or 'full cast'
    * Title ends with ' GA' (GA Productions naming convention, e.g. 'Red Rising Saga GA')
    """
    title_l = (title or "").lower()
    pub_l = (publisher or "").lower()
    narr_l = (narrators or "").lower()
    return (
        "dramatized adaptation" in title_l
        or "graphic audio" in title_l
        or "graphic audio" in pub_l
        or "graphicaudio" in pub_l
        or "graphicaudio" in narr_l
        or "full cast" in narr_l
        or title_l.rstrip().endswith(" ga")
    )


def get_abs_candidates(db: Session) -> list[dict[str, Any]]:
    """Return ABS audiobooks that have NOT yet been imported."""
    conn = _ro_connect(settings.abs_db_path)
    if conn is None:
        return []

    already = {
        r.external_id
        for r in db.query(ExternalImport).filter_by(source="audiobookshelf").all()
    }

    existing = _build_existing(db)

    rows = conn.execute(ABS_QUERY).fetchall()
    conn.close()

    results = []
    seen_ids: set[str] = set()  # guard against JOIN-multiplied duplicate rows
    for row in rows:
        abs_id = row[0]
        if abs_id in already or abs_id in seen_ids:
            continue
        seen_ids.add(abs_id)
        # Strip noise phrases (Unabridged, Dramatized Adaptation, …) from titles
        raw_title = _NOISE_RE.sub('', row[1] or '').strip()
        first, last = _split_author(row[2] or "")
        # duration is in seconds → convert to hours for display
        duration_h = round(row[7] / 3600, 1) if row[7] else None
        abs_series_num = _parse_seq(row[10])
        abs_year = str(row[3]) if row[3] else None
        candidate = {
            "external_id": abs_id,
            "title": raw_title,
            "author": row[2],
            "author_name_first": first,
            "author_name_second": last,
            "date_published": f"{row[3]}-01-01" if row[3] else None,
            "isbn": row[4],
            "asin": row[5],
            "has_cover": bool(row[6]),
            "abs_cover_path": row[6],
            "duration_hours": duration_h,
            "duration_seconds": row[7],
            "word_count": _estimate_word_count(row[7]),
            "genres": row[8],
            "series": row[9],
            "series_number": abs_series_num,
            "publisher": row[11],
            "narrators": row[12],
            "similar_to": _best_match(
                raw_title, row[2] or "", row[9], abs_series_num, abs_year, existing
            ),
        }
        results.append(candidate)
    return _collapse_parts(results, existing)


def _parse_seq(seq: Optional[str]) -> Optional[float]:
    if not seq:
        return None
    m = re.match(r"[\d.]+", seq)
    return float(m.group()) if m else None


# Standard audiobook narration speed used to estimate word counts from duration.
_ABS_WPM = 150


def _estimate_word_count(duration_seconds: Optional[float]) -> Optional[int]:
    """Return an estimated word count from audiobook duration.

    Uses the industry-standard narration rate of 150 WPM (9,000 words/hour).
    Result is rounded to the nearest 1,000 for clarity.
    Returns None when duration is unavailable.
    """
    if not duration_seconds:
        return None
    raw = duration_seconds / 60 * _ABS_WPM
    return int(round(raw / 1000) * 1000) or None


# Strip common noise phrases from ABS titles (case-insensitive)
_NOISE_RE = re.compile(
    r'\s*\((unabridged|dramatized adaptation)\)\s*',
    re.IGNORECASE,
)

# Matches trailing " - Part 1", ", Part 2", " (Part 3 of 5)", " Part 1", etc.
_PART_SUFFIX_RE = re.compile(
    r'\s*[-\u2013,]?\s*\(?(Part|Pt\.?)\s+(\d+)(\s+of\s+\d+)?\)?\s*$',
    re.IGNORECASE,
)


def _strip_part_suffix(title: str) -> tuple[str, Optional[int]]:
    """Return (base_title, part_num), or (title, None) if no part suffix found."""
    m = _PART_SUFFIX_RE.search(title)
    if m:
        return title[:m.start()].strip(), int(m.group(2))
    return title, None


def _collapse_parts(candidates: list[dict], existing: list) -> list[dict]:
    """Collapse multi-part audiobooks and deduplicate same-title/author entries.

    Two passes:
    1. Part collapse — entries whose titles end in "Part N" are grouped,
       their IDs joined with '||', and their durations summed.
    2. Duplicate merge — non-part entries sharing the same normalized
       title+author (different ABS IDs, same book) are merged: IDs joined
       so every part gets marked imported, best metadata kept, no badge shown.
    """
    part_groups: dict[tuple, dict] = {}   # (base_lower, author_lower) → {base_title, parts}
    non_parts: list[dict] = []

    for c in candidates:
        base, part_num = _strip_part_suffix(c["title"])
        if part_num is not None:
            key = (base.lower(), (c.get("author") or "").lower())
            if key not in part_groups:
                part_groups[key] = {"base_title": base, "parts": []}
            part_groups[key]["parts"].append((part_num, c))
        else:
            non_parts.append(c)

    # --- Pass 1: collapse numbered parts ---
    collapsed_parts: list[dict] = []
    for gdata in part_groups.values():
        parts = sorted(gdata["parts"], key=lambda x: x[0])
        base_title = gdata["base_title"]
        primary = parts[0][1]
        abs_year = (primary.get("date_published") or "")[:4] or None

        if len(parts) == 1:
            standalone = dict(primary)
            standalone["title"] = base_title
            standalone["similar_to"] = _best_match(
                base_title, primary.get("author") or "",
                primary.get("series"), primary.get("series_number"), abs_year, existing,
            )
            collapsed_parts.append(standalone)
        else:
            total_dur_h = sum((p[1].get("duration_hours") or 0) for p in parts)
            total_dur_s = sum((p[1].get("duration_seconds") or 0) for p in parts)
            all_ids = [p[1]["external_id"] for p in parts]
            collapsed_parts.append({
                **primary,
                "title": base_title,
                "external_id": "||".join(all_ids),
                "duration_hours": round(total_dur_h, 1) if total_dur_h else None,
                "duration_seconds": total_dur_s or None,
                "word_count": _estimate_word_count(total_dur_s or None),
                "_part_count": len(parts),
                "similar_to": _best_match(
                    base_title, primary.get("author") or "",
                    primary.get("series"), primary.get("series_number"), abs_year, existing,
                ),
            })

    # --- Pass 2: deduplicate non-part entries by (title_lower, author_lower) ---
    dupe_groups: dict[tuple, list[dict]] = {}
    for c in non_parts:
        key = (c["title"].lower(), (c.get("author") or "").lower())
        dupe_groups.setdefault(key, []).append(c)

    deduped_non_parts: list[dict] = []
    for dupes in dupe_groups.values():
        if len(dupes) == 1:
            deduped_non_parts.append(dupes[0])
        else:
            # Pick the entry with the best metadata (prefer one that has a cover)
            best = next((d for d in dupes if d.get("has_cover")), dupes[0])
            all_ids = [d["external_id"] for d in dupes]
            merged = dict(best)
            merged["external_id"] = "||".join(all_ids)
            # Recalculate similarity with the clean title
            abs_year = (best.get("date_published") or "")[:4] or None
            merged["similar_to"] = _best_match(
                best["title"], best.get("author") or "",
                best.get("series"), best.get("series_number"), abs_year, existing,
            )
            deduped_non_parts.append(merged)

    return deduped_non_parts + collapsed_parts


def _find_abs_cover(
    db_cover_path: Optional[str],
    part_ids: list[str],
    abs_metadata_path: str,
) -> Optional[Path]:
    """Locate the best accessible cover image for an ABS item.

    Strategy (in order):
    1. Resolve the DB's coverPath against the ABS data root
       (the parent of abs_metadata_path).  This handles both
       '/metadata/items/<id>/cover.jpg' and custom paths.
    2. Check the ABS metadata items directory for each part ID in order.
    """
    abs_root = Path(abs_metadata_path).parent

    # 1. DB-provided path
    if db_cover_path:
        candidate = abs_root / db_cover_path.lstrip("/")
        if candidate.exists():
            return candidate

    # 2. Metadata items fallback — try every part ID
    for pid in part_ids:
        candidate = Path(abs_metadata_path) / "items" / pid / "cover.jpg"
        if candidate.exists():
            return candidate

    return None


def import_abs_book(
    db: Session,
    abs_id: str,
    *,
    book_id: Optional[int] = None,
    override: Optional[dict] = None,
) -> dict[str, Any]:
    """Import (or link) one ABS audiobook (or a collapsed multi-part group) into GreatReads.

    abs_id may be a single ABS item ID or a '||'-joined list of part IDs.
    When multiple parts are given, metadata is taken from the first part and one
    ExternalImport record is created per part ID.
    """
    # Split multi-part IDs; primary_id supplies the metadata/cover
    part_ids = abs_id.split("||")
    primary_id = part_ids[0]

    conn = _ro_connect(settings.abs_db_path)
    if conn is None:
        raise RuntimeError("ABS DB not accessible")

    row = conn.execute(
        f"""
        SELECT li.id, li.title, li.authorNamesFirstLast,
               b.publishedYear, b.isbn, b.asin, b.coverPath, b.duration, b.genres,
               s.name, bs.sequence,
               b.publisher, b.narrators
        FROM libraryItems li
        JOIN books b ON li.mediaId = b.id
        LEFT JOIN bookSeries bs ON bs.bookId = b.id
        LEFT JOIN series s ON bs.seriesId = s.id
        WHERE li.id = '{primary_id}' AND li.mediaType = 'book'
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    if row is None:
        raise ValueError(f"ABS item {primary_id} not found")

    # --- Graphic Audio detection ---
    # Check primary item first; if multi-part, also scan every part for GA signals.
    # row[11]=publisher, row[12]=narrators (added to query above)
    _primary_is_ga = _is_graphic_audio(row[1], row[11], row[12])
    _ga_detected = _primary_is_ga
    if not _ga_detected and len(part_ids) > 1:
        conn3 = _ro_connect(settings.abs_db_path)
        if conn3:
            placeholders = ",".join(f"\'{pid}\'" for pid in part_ids)
            ga_rows = conn3.execute(
                f"SELECT b.title, b.publisher, b.narrators FROM libraryItems li "
                f"JOIN books b ON li.mediaId=b.id WHERE li.id IN ({placeholders})"
            ).fetchall()
            conn3.close()
            _ga_detected = any(_is_graphic_audio(r[0], r[1], r[2]) for r in ga_rows)

    first, last = _split_author(row[2] or "")
    pub_date = None
    if row[3]:
        try:
            pub_date = datetime.strptime(str(row[3]), "%Y").date().replace(month=1, day=1)
        except ValueError:
            pass

    # Clean title: strip noise phrases and part suffix so we store the base title
    clean_title = _NOISE_RE.sub('', row[1] or '').strip()
    clean_title, _ = _strip_part_suffix(clean_title)

    # row[7] is the primary part's duration; sum all part durations for accurate estimate
    total_duration_s: Optional[float] = None
    if len(part_ids) > 1:
        conn2 = _ro_connect(settings.abs_db_path)
        if conn2:
            placeholders = ",".join(f"'{pid}'" for pid in part_ids)
            dur_rows = conn2.execute(
                f"SELECT b.duration FROM libraryItems li JOIN books b ON li.mediaId = b.id"
                f" WHERE li.id IN ({placeholders})"
            ).fetchall()
            conn2.close()
            total_duration_s = sum(r[0] for r in dur_rows if r[0]) or None
    else:
        total_duration_s = row[7]

    estimated_wc = _estimate_word_count(total_duration_s)

    if book_id is None:
        fields = {
            "title": clean_title,
            "author_name_first": first,
            "author_name_second": last,
            "date_published": pub_date,
            "series": row[9],
            "series_number": _parse_seq(row[10]),
            "word_count": estimated_wc,
            "cover": False,
        }
        if override:
            fields.update(override)

        # Ensure date_published is a proper Python date object (override may send strings)
        fields["date_published"] = _coerce_date(fields.get("date_published"))

        book = Book(**fields)
        db.add(book)
        db.flush()

        # Copy cover: try DB coverPath resolved against ABS data root first,
        # then fall back to checking every part ID in the metadata items dir.
        cover_src = _find_abs_cover(row[6], part_ids, settings.abs_metadata_path)
        if cover_src and _copy_cover(cover_src, book.id):
            book.cover = True

        inv = Inventory(book_id=book.id, owned_audio=True,
                        graphic_audio=_ga_detected, owned_in_library=True)
        db.add(inv)
        action = "created"
    else:
        book = db.query(Book).get(book_id)
        if book is None:
            raise ValueError(f"GreatReads book {book_id} not found")
        if not book.cover:
            cover_src = _find_abs_cover(row[6], part_ids, settings.abs_metadata_path)
            if cover_src and _copy_cover(cover_src, book.id):
                book.cover = True
        # Merge series: if ABS has one and the existing book doesn't, bring it in
        abs_series = row[9]
        abs_series_num = _parse_seq(row[10])
        if abs_series and not book.series:
            book.series = abs_series
            book.series_number = abs_series_num
        # Merge word count: fill in estimated value if missing
        if estimated_wc and not book.word_count:
            book.word_count = estimated_wc
        # Apply explicit user-selected field overrides (take precedence over auto-merge)
        if override:
            for field in ('series', 'series_number', 'universe'):
                if field in override:
                    setattr(book, field, override[field])
            if 'date_published' in override:
                book.date_published = _coerce_date(override['date_published'])
        # Mark audio ownership on the existing inventory row (create one if absent)
        inv = db.query(Inventory).filter_by(book_id=book.id).first()
        if inv is None:
            inv = Inventory(book_id=book.id, owned_audio=True)
            db.add(inv)
        else:
            inv.owned_audio = True
        # Set GA flags when detected; never clear an already-set graphic_audio flag.
        if _ga_detected:
            inv.graphic_audio = True
            # owned_in_library stays True when author matches; False flags for review.
            book_author = ' '.join(
                filter(None, [book.author_name_first, book.author_name_second])
            ).lower()
            abs_author = (row[2] or '').lower()
            authors_share_token = bool(
                _word_tokens(book_author) & _word_tokens(abs_author)
            ) if book_author and abs_author else True
            if not authors_share_token:
                inv.owned_in_library = False
                logger.warning(
                    "GA book %s linked to GR%s ('%s') but author mismatch: "
                    "GR='%s' vs ABS='%s' — flagged owned_in_library=0 for review",
                    primary_id, book.id, book.title, book_author, abs_author,
                )
        action = "linked"

    # Create one ExternalImport record per part ID (skip any already recorded)
    already_recorded = {
        r.external_id
        for r in db.query(ExternalImport)
        .filter(
            ExternalImport.source == "audiobookshelf",
            ExternalImport.external_id.in_(part_ids),
        )
        .all()
    }
    for pid in part_ids:
        if pid not in already_recorded:
            db.add(ExternalImport(
                source="audiobookshelf",
                external_id=pid,
                book_id=book.id,
                action=action,
                imported_at=datetime.utcnow(),
            ))
    db.commit()
    db.refresh(book)
    return book.to_dict()

