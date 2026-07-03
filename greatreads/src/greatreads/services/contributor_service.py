"""Book contributors (#192) — primary + additional authors & narrators.

Source of truth for the "(+N)" display and for author/narrator search that spans
BOTH primary and secondary roles. The primary author/narrator are also mirrored back
onto the Book (author_name_*, narrator) so existing card rendering keeps working.
"""

import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.book import Book
from ..models.book_contributor import BookContributor
from .import_service import _split_author

logger = logging.getLogger(__name__)

AUTHOR, NARRATOR = "author", "narrator"


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


def contributors_for(db: Session, book_id: int) -> dict:
    """{'authors': [...], 'narrators': [...]} ordered primary-first for a book."""
    rows = (db.query(BookContributor)
            .filter(BookContributor.book_id == book_id)
            .order_by(BookContributor.role, BookContributor.is_primary.desc(),
                      BookContributor.position, BookContributor.id).all())
    out = {"authors": [], "narrators": []}
    for r in rows:
        (out["authors"] if r.role == AUTHOR else out["narrators"]).append(r.to_dict())
    return out


def _pairs_from_names(names) -> list:
    """['Kate Reading', ...] → [(first, last), ...] (skips blanks/dupes)."""
    seen, pairs = set(), []
    for n in names or []:
        n = (n or "").strip()
        if not n:
            continue
        f, l = _split_author(n)
        key = _norm(f) + "|" + _norm(l)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((f or None, l or None))
    return pairs


def set_book_contributors(db: Session, book_id: int, authors: list, narrators: list) -> dict:
    """Replace a book's contributors from the edit modal. ``authors`` / ``narrators`` are
    ordered lists of {first,last} (or full-name strings); index 0 = primary. Mirrors the
    primary back onto the Book. Commits."""
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise ValueError("book not found")

    def _clean(items):
        out = []
        for it in items or []:
            if isinstance(it, dict):
                f = (it.get("first") or "").strip()
                l = (it.get("last") or "").strip()
                if not (f or l) and it.get("name"):
                    f, l = _split_author(it["name"])
            else:
                f, l = _split_author(str(it))
            if f or l:
                out.append((f or None, l or None))
        return out

    a_pairs, n_pairs = _clean(authors), _clean(narrators)

    db.query(BookContributor).filter(BookContributor.book_id == book_id).delete(synchronize_session=False)
    for role, pairs in ((AUTHOR, a_pairs), (NARRATOR, n_pairs)):
        for i, (f, l) in enumerate(pairs):
            db.add(BookContributor(book_id=book_id, role=role, first=f, last=l,
                                   is_primary=(i == 0), position=i))

    # Mirror primary onto the Book for card rendering.
    if a_pairs:
        book.author_name_first, book.author_name_second = a_pairs[0]
    book.narrator = ", ".join(" ".join(p for p in pr if p) for pr in n_pairs) or None
    db.commit()
    return contributors_for(db, book_id)


def backfill_all(db: Session) -> dict:
    """One-time: seed book_contributors from the existing primary author + narrator(s).
    Idempotent — skips books that already have any contributor row."""
    have = {bid for (bid,) in db.query(BookContributor.book_id).distinct().all()}
    made = 0
    for book in db.query(Book).all():
        if book.id in have:
            continue
        rows = []
        if book.author_name_first or book.author_name_second:
            rows.append(BookContributor(book_id=book.id, role=AUTHOR,
                                        first=book.author_name_first, last=book.author_name_second,
                                        is_primary=True, position=0))
        if book.narrator:
            for i, (f, l) in enumerate(_pairs_from_names([n.strip() for n in str(book.narrator).split(",")])):
                rows.append(BookContributor(book_id=book.id, role=NARRATOR, first=f, last=l,
                                            is_primary=(i == 0), position=i))
        if rows:
            db.add_all(rows)
            made += len(rows)
    if made:
        db.commit()
    return {"contributors_added": made}


def _book_ids_by_contributor(db: Session, role: str, name: str) -> set:
    """book_ids where `name` appears in `role` (primary OR secondary), tolerant of
    'First Last' vs stored first/last."""
    f, l = _split_author(name or "")
    q = db.query(BookContributor.book_id).filter(BookContributor.role == role)
    full = func.trim(func.coalesce(BookContributor.first, "") + " " + func.coalesce(BookContributor.last, ""))
    conds = [func.lower(full) == _norm(name)]
    if l:
        conds.append(func.lower(func.coalesce(BookContributor.last, "")) == _norm(l))
    from sqlalchemy import or_
    q = q.filter(or_(*conds))
    return {bid for (bid,) in q.all()}
