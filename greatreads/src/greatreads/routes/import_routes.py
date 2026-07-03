"""API routes for importing books from Calibre and Audiobookshelf."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.book import Book
from ..models.external_import import ExternalImport
from ..services.import_service import (
    get_calibre_candidates,
    get_abs_candidates,
    import_calibre_book,
    import_abs_book,
    get_calibre_cover_path,
    get_abs_cover_path,
    backfill_calibre_word_counts,
    backfill_abs_word_counts,
    refresh_calibre_metadata,
)
from ..services.sync_service import sync_all

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------

class ImportRequest(BaseModel):
    """Body for create-or-link actions."""
    book_id: Optional[int] = None          # None → create new, int → link existing
    override: Optional[Dict[str, Any]] = None   # field overrides when creating


# ---------------------------------------------------------------------------
# Status endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
async def import_status(db: Session = Depends(get_db)):
    """Return counts of already-imported records per source."""
    calibre_count = (
        db.query(ExternalImport).filter_by(source="calibre").count()
    )
    abs_count = (
        db.query(ExternalImport).filter_by(source="audiobookshelf").count()
    )
    return {"calibre_imported": calibre_count, "abs_imported": abs_count}


@router.post("/sync")
async def trigger_sync(db: Session = Depends(get_db)):
    """Force an immediate import of any new Calibre/ABS items.

    Runs the same job the scheduler runs every few minutes. Returns a summary
    of how many books were created vs linked per source.
    """
    return sync_all(db)


@router.post("/refresh-calibre")
async def trigger_calibre_refresh(db: Session = Depends(get_db)):
    """Backfill empty GreatReads fields (date/word count/series/universe) from Calibre
    for already-linked books (#147) — fixes books imported with sparse metadata that
    Calibre has since completed. Never overwrites non-empty (user-edited) values."""
    return refresh_calibre_metadata(db)


# ---------------------------------------------------------------------------
# Newly-Imported "dismissed" set — persisted server-side (#181)
# ---------------------------------------------------------------------------
# Dismissal used to live only in browser localStorage, so it never survived a
# refresh from another device/origin (phone vs. the forge-freedom proxy). We now
# keep the reviewed import_ids in user_settings (migration-free — no schema change,
# and we deliberately DON'T delete the external_imports rows: they're the import
# ledger and #179 auto-fulfill confirmation reads them).
_DISMISSED_KEY = "imports_dismissed"


def _get_dismissed(db: Session) -> set[int]:
    from ..models.user_settings import UserSettings
    import json
    s = db.query(UserSettings).filter(UserSettings.setting_key == _DISMISSED_KEY).first()
    if not s or not s.setting_value:
        return set()
    try:
        return {int(x) for x in json.loads(s.setting_value)}
    except (ValueError, TypeError):
        return set()


def _set_dismissed(db: Session, ids: set[int]) -> None:
    from ..models.user_settings import UserSettings
    import json
    val = json.dumps(sorted(ids))
    s = db.query(UserSettings).filter(UserSettings.setting_key == _DISMISSED_KEY).first()
    if s:
        s.setting_value = val
    else:
        db.add(UserSettings(setting_key=_DISMISSED_KEY, setting_value=val))
    db.commit()


class DismissRequest(BaseModel):
    import_ids: List[int]


@router.post("/dismiss")
async def dismiss_imports(payload: DismissRequest, db: Session = Depends(get_db)):
    """Mark import rows reviewed so they drop from the Newly-Imported tray + badge.
    Accepts a list so it serves single-dismiss, dismiss-all, and one-time migration
    of a browser's old localStorage set. Persistent + cross-device (#181)."""
    dismissed = _get_dismissed(db)
    before = len(dismissed)
    dismissed.update(int(i) for i in (payload.import_ids or []))
    if len(dismissed) != before:
        _set_dismissed(db, dismissed)
    return {"dismissed": len(dismissed), "added": len(dismissed) - before}


@router.get("/recent")
async def recent_imports(limit: int = 50, db: Session = Depends(get_db)):
    """Recently auto-imported ebooks/audiobooks (newest first) for the Library
    'Newly Imported' tray (#137). Only external (Calibre/ABS) imports appear —
    physical books are user-added. Each item reports whether it CREATED a new
    book or LINKED into an existing one, and flags a possible duplicate when
    another book shares its normalized title+author (so mis-imports like the
    Shannara set surface for review/merge).

    Reviewed (dismissed) rows are excluded server-side so the tray stays cleared
    across refreshes and devices (#181)."""
    from sqlalchemy import func
    from ..models.inventory import Inventory

    dismissed = _get_dismissed(db)
    rows = (
        db.query(ExternalImport)
        .order_by(ExternalImport.imported_at.desc().nullslast())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    out = []
    for e in rows:
        if e.id in dismissed:
            continue
        bk = db.query(Book).filter(Book.id == e.book_id).first()
        if not bk:
            continue
        inv = db.query(Inventory).filter_by(book_id=bk.id).first()
        media = []
        if inv:
            if inv.owned_ebook:
                media.append("Ebook")
            if inv.owned_audio:
                media.append("Audio")
            if inv.owned_physical:
                media.append("Physical")
        dup = None
        if bk.title and (bk.title or "").strip():
            t = (bk.title or "").strip().lower()
            a = ((bk.author_name_first or "") + " " + (bk.author_name_second or "")).strip().lower()
            for other in (
                db.query(Book)
                .filter(Book.id != bk.id, func.lower(func.trim(Book.title)) == t)
                .all()
            ):
                oa = ((other.author_name_first or "") + " " + (other.author_name_second or "")).strip().lower()
                if oa == a:
                    dup = {"id": other.id, "title": other.title}
                    break
        out.append({
            "import_id": e.id,
            "source": e.source,
            "external_id": e.external_id,
            "action": e.action,
            "imported_at": e.imported_at.isoformat() if e.imported_at else None,
            "book_id": bk.id,
            "title": bk.title,
            "author": bk.author,
            "series": bk.series,
            "series_number": bk.series_number,
            "cover": bool(bk.cover),
            "media_owned": media,
            "possible_duplicate": dup,
        })
    return out


# ---------------------------------------------------------------------------
# Calibre
# ---------------------------------------------------------------------------

@router.get("/calibre/{calibre_id}/cover")
async def calibre_cover(calibre_id: str):
    """Serve a Calibre book's cover image for preview in the import UI."""
    path = get_calibre_cover_path(calibre_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/abs/{abs_id:path}/cover")
async def abs_cover(abs_id: str):
    """Serve an ABS item's cover image for preview in the import UI."""
    path = get_abs_cover_path(abs_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/calibre/candidates", response_model=List[Dict[str, Any]])
async def calibre_candidates(db: Session = Depends(get_db)):
    """Return Calibre books not yet imported into GreatReads."""
    try:
        return get_calibre_candidates(db)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/calibre/backfill-word-counts")
async def backfill_word_counts(db: Session = Depends(get_db)):
    """Backfill word_count for all Calibre-imported books currently missing it."""
    try:
        result = backfill_calibre_word_counts(db)
        return {"status": "ok", **result}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/calibre/{calibre_id}")
async def import_from_calibre(
    calibre_id: str,
    body: ImportRequest,
    db: Session = Depends(get_db),
):
    """
    Import or link a single Calibre book.

    - body.book_id = None   → create a new GreatReads book
    - body.book_id = <int>  → link this Calibre record to that existing book
    """
    # Prevent duplicate imports
    existing = (
        db.query(ExternalImport)
        .filter_by(source="calibre", external_id=calibre_id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Calibre book {calibre_id} already imported (book_id={existing.book_id})",
        )
    try:
        book = import_calibre_book(
            db,
            calibre_id,
            book_id=body.book_id,
            override=body.override,
        )
        return {"status": "ok", "book": book}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/calibre/{calibre_id}")
async def undo_calibre_import(calibre_id: str, db: Session = Depends(get_db)):
    """Remove the import record (book itself is NOT deleted)."""
    record = (
        db.query(ExternalImport)
        .filter_by(source="calibre", external_id=calibre_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Import record not found")
    db.delete(record)
    db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Audiobookshelf
# ---------------------------------------------------------------------------

@router.get("/abs/candidates", response_model=List[Dict[str, Any]])
async def abs_candidates(db: Session = Depends(get_db)):
    """Return ABS audiobooks not yet imported into GreatReads."""
    try:
        return get_abs_candidates(db)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/abs/backfill-word-counts")
async def abs_backfill_word_counts(db: Session = Depends(get_db)):
    """Estimate word counts for all ABS-imported books that have none."""
    try:
        result = backfill_abs_word_counts(db)
        return {"status": "ok", **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/abs/{abs_id:path}")
async def import_from_abs(
    abs_id: str,
    body: ImportRequest,
    db: Session = Depends(get_db),
):
    """Import or link a single ABS audiobook."""
    existing = (
        db.query(ExternalImport)
        .filter_by(source="audiobookshelf", external_id=abs_id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"ABS item {abs_id} already imported (book_id={existing.book_id})",
        )
    try:
        book = import_abs_book(
            db,
            abs_id,
            book_id=body.book_id,
            override=body.override,
        )
        return {"status": "ok", "book": book}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/abs/{abs_id:path}")
async def undo_abs_import(abs_id: str, db: Session = Depends(get_db)):
    """Remove the ABS import record (book itself is NOT deleted)."""
    record = (
        db.query(ExternalImport)
        .filter_by(source="audiobookshelf", external_id=abs_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Import record not found")
    db.delete(record)
    db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Existing books list (for the "Link to existing" picker)
# ---------------------------------------------------------------------------

@router.get("/books/search")
async def search_existing_books(
    q: str = "",
    db: Session = Depends(get_db),
):
    """Quick search of existing GreatReads books for the link-to-existing picker."""
    query = db.query(Book)
    if q:
        query = query.filter(Book.title.ilike(f"%{q}%"))
    books = query.order_by(Book.title).limit(50).all()
    return [
        {
            "id": b.id,
            "title": b.title,
            "author": b.author,
            "series": b.series,
            "series_number": b.series_number,
            "cover": b.cover,
        }
        for b in books
    ]

