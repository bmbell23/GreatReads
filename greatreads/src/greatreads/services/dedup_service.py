"""Wishlist de-dup + blank-row cleanup (#216).

Wishlist = a Book with no owned Inventory copy and no ExternalImport (the #185 "library"
definition, same as news_service / metadata_backfill_service). Some Wishlist rows are
duplicates of books you already own ('Mindset' vs 'Mindset: The New Psychology of
Success'); this finds them with the importer's own matcher so Settings can offer the
#131 merge compare. Also finds/purges the blank-title junk rows."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import settings
from ..models.book import Book
from ..models.book_contributor import BookContributor
from ..models.external_import import ExternalImport
from ..models.inventory import Inventory
from ..models.reading import Reading
from .import_service import _best_match, _build_existing

# 'Likely duplicate' bar from _best_match's docstring. Subtitle cases ('Mindset' vs
# 'Mindset: The New Psychology of Success') clear it via the #204 core-token path. (#216)
DUP_THRESHOLD = 0.85


def _owned_ids(db: Session) -> set[int]:
    """Book ids that are in the library: an owned inventory copy OR an import (#185)."""
    inv = db.query(Inventory.book_id).filter(
        or_(Inventory.owned_ebook == True, Inventory.owned_audio == True,  # noqa: E712
            Inventory.owned_physical == True))
    ext = db.query(ExternalImport.book_id)
    return {r[0] for r in inv.all()} | {r[0] for r in ext.all()}


def _author(b) -> str:
    return " ".join(filter(None, [b.author_name_first, b.author_name_second]))


def find_duplicates(db: Session, threshold: float = DUP_THRESHOLD) -> list[dict]:
    """Wishlist books that match an OWNED book at >= threshold, best score first."""
    owned = _owned_ids(db)
    existing = [e for e in _build_existing(db) if e[0] in owned]   # candidates: owned only
    by_id = {e[0]: e for e in existing}
    pairs = []
    for b in db.query(Book).order_by(Book.id).all():
        if b.id in owned or not (b.title or "").strip():
            continue
        author = _author(b)
        m = _best_match(b.title, author, b.series, b.series_number,
                        str(b.date_published.year) if b.date_published else None, existing)
        if not m or m["id"] == b.id or m["id"] not in by_id or m["score"] < threshold:
            continue
        pairs.append({
            "wishlist": {"id": b.id, "title": b.title, "author": author},
            "owned": {"id": m["id"], "title": m["title"], "author": by_id[m["id"]][2]},
            "score": m["score"],
        })
    pairs.sort(key=lambda p: (-p["score"], p["wishlist"]["id"]))
    return pairs


def find_blank(db: Session) -> list[dict]:
    """Rows with a NULL/whitespace title; `deletable` = not owned, no import, no readings."""
    owned = _owned_ids(db)
    imported = {r[0] for r in db.query(ExternalImport.book_id).all()}
    out = []
    for b in db.query(Book).order_by(Book.id).all():
        if (b.title or "").strip():
            continue
        n_read = db.query(Reading).filter(Reading.book_id == b.id).count()
        is_owned = b.id in owned
        has_imp = b.id in imported
        out.append({
            "id": b.id, "author": _author(b),
            "date_published": b.date_published.isoformat() if b.date_published else None,
            "owned": is_owned, "has_import": has_imp, "readings": n_read,
            "deletable": not is_owned and not has_imp and n_read == 0,
        })
    return out


def backup_db(db_path: str, backup_dir: Path) -> str:
    """Online-backup the SQLite file (safe on a live DB). Raises on any failure."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst_path = backup_dir / f"greatreads-pre-purge-blank-{ts}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return str(dst_path)


def purge_blank(db: Session, db_path: str, backup_dir: Optional[Path] = None) -> dict:
    """Back up the DB, then delete ONLY the deletable blank rows (recomputed here).
    Backup failure raises before anything is touched."""
    ids = [r["id"] for r in find_blank(db) if r["deletable"]]
    backup = backup_db(db_path, backup_dir or Path(db_path).parent / "backups")
    for bid in ids:
        book = db.query(Book).filter(Book.id == bid).first()
        if not book:
            continue
        # book_contributors has no cascade — delete them or they orphan (#290).
        db.query(BookContributor).filter(BookContributor.book_id == bid).delete(
            synchronize_session=False)
        book.tags = []   # clears the book_tags m2m rows
        db.delete(book)  # cascades readings + inventory
    db.commit()
    for bid in ids:   # tidy cover files like DELETE /{book_id} (#95)
        for p in (settings.covers_dir / f"{bid}.jpg",
                  Path("/app/data/covers_thumb") / f"{bid}.jpg"):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return {"deleted": ids, "backup": backup}
