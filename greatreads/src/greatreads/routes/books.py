"""Book management API routes."""

import os
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request, Body
from sqlalchemy.orm import Session
import shutil
from pathlib import Path
import httpx
from pydantic import BaseModel

from ..database import get_db
from ..models.book import Book, BookCreate, BookUpdate, BookResponse
from ..models.reading import Reading
from ..models.tag import Tag
from ..models.inventory import Inventory
from ..models.external_import import ExternalImport
from ..models.user import User
from ..config import settings
from ..auth import get_current_user
from ._book_enrich import enrich_book_dict

router = APIRouter()


def get_or_create_tags(db: Session, tag_names: List[str]) -> List[Tag]:
    """Get existing tags or create new ones."""
    tags = []
    for tag_name in tag_names:
        tag_name = tag_name.strip()
        if not tag_name:
            continue
        # Try to find existing tag (case-insensitive)
        tag = db.query(Tag).filter(Tag.name.ilike(tag_name)).first()
        if not tag:
            # Create new tag
            tag = Tag(name=tag_name)
            db.add(tag)
            db.flush()  # Flush to get the ID
        tags.append(tag)
    return tags


@router.get("/")
async def get_books(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    search: Optional[str] = Query(None),
    author: Optional[str] = Query(None),
    series: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get books with optional filtering."""
    query = db.query(Book)

    if search:
        query = query.filter(Book.title.ilike(f"%{search}%"))

    if author:
        query = query.filter(
            (Book.author_name_first.ilike(f"%{author}%")) |
            (Book.author_name_second.ilike(f"%{author}%"))
        )

    if series:
        query = query.filter(Book.series.ilike(f"%{series}%"))

    if genre:
        query = query.filter(Book.genre.ilike(f"%{genre}%"))

    books = query.offset(skip).limit(limit).all()

    # Enrich with reading information
    enriched_books = []
    for book in books:
        book_data = book.to_dict()
        # Check if book has been read
        readings = db.query(Reading).filter(Reading.book_id == book.id).all()
        book_data["is_read"] = any(r.date_finished_actual for r in readings)

        # Add cover version for cache busting
        if book.cover:
            cover_path = settings.covers_dir / f"{book.id}.jpg"
            if cover_path.exists():
                book_data["cover_version"] = int(os.path.getmtime(cover_path))
            else:
                book_data["cover_version"] = 0
        else:
            book_data["cover_version"] = 0

        enriched_books.append(book_data)

    return enriched_books


@router.get("/{book_id}")
async def get_book(
    request: Request,
    book_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific book."""
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    book_dict = book.to_dict()

    # Add inventory data separately to avoid circular references
    if book.inventory:
        book_dict["inventory"] = [{
            "id": inv.id,
            "book_id": inv.book_id,
            "owned_audio": inv.owned_audio,
            "owned_ebook": inv.owned_ebook,
            "owned_physical": inv.owned_physical,
            "date_purchased": inv.date_purchased.isoformat() if inv.date_purchased else None,
            "location": inv.location,
            "read_status": inv.read_status,
            "read_count": inv.read_count,
            "isbn_10": inv.isbn_10,
            "isbn_13": inv.isbn_13,
        } for inv in book.inventory]
    else:
        book_dict["inventory"] = []

    return book_dict


@router.get("/{book_id}/details")
async def get_book_details(
    request: Request,
    book_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Fully-enriched book for the shared book popup (#120 Phase 2): the same shape the
    Library/TBR feeds pass to GreatReads.openBookActions — book fields + author + readings
    + inventory + calibre_id/abs_id — so the popup can open ANY book by id (series siblings,
    author's other books) without the page having loaded it."""
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    d = book.to_dict()   # includes id/title/author/series/universe/series_number/counts
    readings = db.query(Reading).filter(Reading.book_id == book_id).all()
    d["readings"] = [r.to_dict() for r in readings]
    enrich_book_dict(d, book.id, db)   # inventory, media_owned, calibre_id, abs_id, read_count
    return d
@router.post("/", response_model=BookResponse)
async def create_book(
    request: Request,
    book: BookCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new book."""
    book_data = book.model_dump(exclude={'tags'})
    db_book = Book(**book_data)

    # Handle tags
    if book.tags:
        db_book.tags = get_or_create_tags(db, book.tags)

    db.add(db_book)
    db.commit()
    db.refresh(db_book)
    # Serialize via to_dict so tags render as names — returning the ORM object would
    # make BookResponse's List[str] tags validation choke on Tag objects (500).
    return db_book.to_dict()


@router.put("/{book_id}", response_model=BookResponse)
async def update_book(
    request: Request,
    book_id: int,
    book: BookUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a book."""
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    update_data = book.model_dump(exclude_unset=True, exclude={'tags'})
    for field, value in update_data.items():
        setattr(db_book, field, value)

    # Handle tags if provided
    if book.tags is not None:
        db_book.tags = get_or_create_tags(db, book.tags)

    db.commit()
    db.refresh(db_book)
    return db_book.to_dict()   # tags as names → avoids BookResponse List[str] 500


@router.delete("/{book_id}")
async def delete_book(
    request: Request,
    book_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a book (cascades readings + inventory; also clears cover files)."""
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    db.delete(db_book)
    db.commit()
    # tidy up cover files so we don't leave orphans behind (#95)
    for p in (settings.covers_dir / f"{book_id}.jpg",
              Path("/app/data/covers_thumb") / f"{book_id}.jpg"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    return {"message": "Book deleted successfully"}


class MergeRequest(BaseModel):
    """Merge the loser book into the survivor (#131). ``fields`` optionally carries
    per-field chosen values to write onto the survivor (field-by-field compare)."""
    survivor_id: int
    loser_id: int
    fields: Optional[dict] = None


# Scalar book columns the merge can carry over / let the user pick.
_MERGE_SCALARS = (
    "title", "author_name_first", "author_name_second", "author_gender",
    "word_count", "page_count", "date_published", "universe", "series",
    "series_number", "genre", "isbn_id",
)


@router.post("/merge")
async def merge_books(
    request: Request,
    body: MergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merge ``loser_id`` into ``survivor_id`` (#131): OR inventory ownership, move
    external links + readings + tags onto the survivor, fill/override survivor scalar
    fields, adopt the loser's cover when the survivor lacks one, then delete the loser.

    reading_sessions / reading_activity / ereader_progress / ereader_highlights key off
    the external (Calibre/ABS) id, not the GreatReads book id, so they follow the moved
    external_imports automatically — no repoint needed."""
    if body.survivor_id == body.loser_id:
        raise HTTPException(status_code=400, detail="Cannot merge a book into itself")
    survivor = db.query(Book).filter(Book.id == body.survivor_id).first()
    loser = db.query(Book).filter(Book.id == body.loser_id).first()
    if not survivor or not loser:
        raise HTTPException(status_code=404, detail="Book not found")

    chosen = body.fields or {}

    # 1) Inventory — OR ownership into the survivor (create a row if it has none),
    #    coalescing ISBNs / shelf location from the loser where the survivor is blank.
    s_inv = db.query(Inventory).filter_by(book_id=survivor.id).first()
    l_inv = db.query(Inventory).filter_by(book_id=loser.id).first()
    if l_inv:
        if not s_inv:
            s_inv = Inventory(book_id=survivor.id)
            db.add(s_inv)
        s_inv.owned_audio = bool(s_inv.owned_audio or l_inv.owned_audio)
        s_inv.owned_ebook = bool(s_inv.owned_ebook or l_inv.owned_ebook)
        s_inv.owned_physical = bool(s_inv.owned_physical or l_inv.owned_physical)
        s_inv.graphic_audio = bool(getattr(s_inv, "graphic_audio", False) or getattr(l_inv, "graphic_audio", False))
        s_inv.owned_in_library = bool(getattr(s_inv, "owned_in_library", False) or getattr(l_inv, "owned_in_library", False))
        for f in ("isbn_10", "isbn_13", "location", "shelf_bookshelf",
                  "shelf_shelf", "shelf_position", "date_purchased"):
            if getattr(s_inv, f, None) in (None, "") and getattr(l_inv, f, None) not in (None, ""):
                setattr(s_inv, f, getattr(l_inv, f))
        db.delete(l_inv)

    # 2) Move external links + readings onto the survivor.
    for ext in db.query(ExternalImport).filter_by(book_id=loser.id).all():
        ext.book_id = survivor.id
        ext.action = ext.action or "linked"
    for rd in db.query(Reading).filter_by(book_id=loser.id).all():
        rd.book_id = survivor.id
    # Tags (m2m): union onto the survivor.
    for t in list(loser.tags):
        if t not in survivor.tags:
            survivor.tags.append(t)
    loser.tags = []

    # 3) Scalar fields: apply user-chosen values, else fill survivor gaps from loser.
    for f in _MERGE_SCALARS:
        if f in chosen:
            setattr(survivor, f, chosen[f])
        elif getattr(survivor, f, None) in (None, "", 0) and getattr(loser, f, None) not in (None, "", 0):
            setattr(survivor, f, getattr(loser, f))

    # 4) Cover: adopt the loser's if the survivor has none (or the user picked it).
    if (chosen.get("cover") == "loser" or not survivor.cover) and loser.cover:
        src = settings.covers_dir / f"{loser.id}.jpg"
        dst = settings.covers_dir / f"{survivor.id}.jpg"
        try:
            if src.exists():
                shutil.copyfile(src, dst)
                survivor.cover = True
        except Exception:
            pass

    # Flush the repoints, then reload the loser so its (now-empty) relationship
    # collections don't cascade-delete the rows we just moved, and delete it.
    db.flush()
    db.expire(loser)
    db.delete(loser)
    db.commit()

    for p in (settings.covers_dir / f"{loser.id}.jpg",
              Path("/app/data/covers_thumb") / f"{loser.id}.jpg"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    return {"message": "merged", "survivor_id": survivor.id, "removed_id": loser.id}


class BulkUpdateRequest(BaseModel):
    """Bulk-edit payload (#93): apply the given fields to every listed book.
    Only the fields present here are written; omit a field to leave it untouched."""
    ids: List[int]
    title: Optional[str] = None
    author_name_first: Optional[str] = None
    author_name_second: Optional[str] = None
    series: Optional[str] = None
    series_number: Optional[float] = None
    universe: Optional[str] = None
    genre: Optional[str] = None
    # Genres as a set (#160): union into each book's genres ('add') or overwrite them
    # ('replace'). Sent alongside/instead of the scalar fields.
    genres: Optional[List[str]] = None
    genres_mode: Optional[str] = None   # 'add' | 'replace'


class IdsRequest(BaseModel):
    ids: List[int]


@router.post("/genres-summary")
async def genres_summary(
    request: Request,
    payload: IdsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Genres across a selection (#160): which are on ALL the books (common) vs only
    some (partial), so the bulk-edit screen can show the shared starting point."""
    books = db.query(Book).filter(Book.id.in_(payload.ids)).all()
    n = len(books)
    if not n:
        return {"count": 0, "common": [], "partial": []}
    from collections import Counter
    cnt: Counter = Counter()
    for b in books:
        for t in b.tags:
            cnt[t.name] += 1
    common = sorted([name for name, c in cnt.items() if c == n], key=str.lower)
    partial = sorted([name for name, c in cnt.items() if c < n], key=str.lower)
    return {"count": n, "common": common, "partial": partial}


@router.post("/bulk-update")
async def bulk_update_books(
    request: Request,
    payload: BulkUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Set shared field(s) across many books at once (#93). A field is applied only if
    it was sent (exclude_unset); send an empty string to deliberately clear a field.
    Genres (#160) apply as a set: 'add' unions the given names into each book, 'replace'
    overwrites each book's genres with exactly the given set (empty = clear)."""
    fields = payload.model_dump(exclude_unset=True, exclude={'ids', 'genres', 'genres_mode'})
    # treat "" as an explicit clear → None; absent fields were already dropped above
    fields = {k: (None if v == "" else v) for k, v in fields.items()}
    # genres apply when a mode was chosen (replace can legitimately clear with [] )
    apply_genres = payload.genres_mode in ("add", "replace")
    if not payload.ids or (not fields and not apply_genres):
        return {"updated": 0}
    books = db.query(Book).filter(Book.id.in_(payload.ids)).all()
    new_tags = get_or_create_tags(db, payload.genres or []) if apply_genres else []
    for b in books:
        for k, v in fields.items():
            setattr(b, k, v)
        if apply_genres:
            if payload.genres_mode == "replace":
                b.tags = list(new_tags)
                b.genre = (payload.genres or [None])[0] if payload.genres else None
            else:  # union, never removing existing
                have = {t.name.lower() for t in b.tags}
                for t in new_tags:
                    if t.name.lower() not in have:
                        b.tags.append(t)
                if not b.genre and payload.genres:
                    b.genre = payload.genres[0]
    db.commit()
    return {"updated": len(books), "fields": list(fields.keys()),
            "genres_mode": payload.genres_mode}


@router.post("/bulk-enrich")
async def bulk_enrich_books(
    request: Request,
    payload: IdsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """On-demand metadata backfill for a selection (#159): fill ONLY empty synopsis/
    genre/public-rating/date/pages from Apple/Google/OpenLibrary — never clobbering
    existing values. Returns {processed, updated, fields_filled}."""
    from ..services.metadata_backfill_service import backfill_ids
    return backfill_ids(db, payload.ids)


class BulkDeleteRequest(BaseModel):
    """Bulk-delete payload (#102): delete every listed book."""
    ids: List[int]


@router.post("/bulk-delete")
async def bulk_delete_books(
    request: Request,
    payload: BulkDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete many books at once (#102). Cascades readings + inventory and clears
    cover files, same as the single-book DELETE."""
    if not payload.ids:
        return {"deleted": 0}
    books = db.query(Book).filter(Book.id.in_(payload.ids)).all()
    deleted_ids = [b.id for b in books]
    for b in books:
        db.delete(b)
    db.commit()
    # tidy up cover files so we don't leave orphans behind (#95)
    for book_id in deleted_ids:
        for p in (settings.covers_dir / f"{book_id}.jpg",
                  Path("/app/data/covers_thumb") / f"{book_id}.jpg"):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return {"deleted": len(deleted_ids)}


@router.post("/{book_id}/cover")
async def upload_book_cover(
    request: Request,
    book_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Upload a cover image for a book."""
    # Check if book exists
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Ensure covers directory exists
    covers_dir = settings.covers_dir
    covers_dir.mkdir(parents=True, exist_ok=True)

    # Save the file as {book_id}.jpg
    file_path = covers_dir / f"{book_id}.jpg"

    try:
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Update book to indicate it has a cover
        db_book.cover = True
        db.commit()

        # Get file modification time for cache busting
        cover_version = int(os.path.getmtime(file_path))

        return {
            "message": "Cover uploaded successfully",
            "book_id": book_id,
            "cover_version": cover_version
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload cover: {str(e)}")
    finally:
        file.file.close()


class CoverUrlRequest(BaseModel):
    """Request model for downloading cover from URL."""
    url: str


@router.post("/{book_id}/cover/from-url")
async def download_cover_from_url(
    request: Request,
    book_id: int,
    cover_request: CoverUrlRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Download and save a cover image from a URL."""
    # Check if book exists
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Ensure covers directory exists
    covers_dir = settings.covers_dir
    covers_dir.mkdir(parents=True, exist_ok=True)

    # Save the file as {book_id}.jpg
    file_path = covers_dir / f"{book_id}.jpg"

    # A real browser-style User-Agent + redirect following; many image hosts
    # (Wikimedia, Amazon, Goodreads) 403 the default httpx UA, and some URLs
    # redirect to a CDN. (#98)
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 GreatReads/cover-fetch"),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": str(cover_request.url),
    }
    try:
        # Download the image
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=headers) as client:
            response = await client.get(cover_request.url)
            response.raise_for_status()

            # Guard against saving an HTML error page as a .jpg
            ctype = response.headers.get("content-type", "")
            if not ctype.lower().startswith("image/"):
                raise HTTPException(status_code=400,
                                    detail=f"URL did not return an image (got '{ctype or 'unknown'}').")

            # Save the image
            with file_path.open("wb") as f:
                f.write(response.content)

        # Update book to indicate it has a cover
        db_book.cover = True
        db.commit()

        # Get file modification time for cache busting
        cover_version = int(os.path.getmtime(file_path))

        return {
            "message": "Cover downloaded and saved successfully",
            "book_id": book_id,
            "cover_version": cover_version
        }
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download image: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save cover: {str(e)}")


@router.delete("/{book_id}/cover")
async def delete_book_cover(
    request: Request,
    book_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove a book's cover (delete the image + thumbnail, clear the flag) so it falls
    back to the placeholder. Used to drop a wrong cover (#88)."""
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")
    for p in (settings.covers_dir / f"{book_id}.jpg",
              Path("/app/data/covers_thumb") / f"{book_id}.jpg"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    db_book.cover = False
    db.commit()
    return {"message": "Cover removed", "book_id": book_id}


@router.get("/search/authors")
async def get_authors(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get list of all authors."""
    authors = db.query(Book.author_name_first, Book.author_name_second).distinct().all()
    author_list = []
    for first, last in authors:
        if first or last:
            name_parts = [first, last]
            full_name = " ".join(filter(None, name_parts))
            author_list.append(full_name)
    return sorted(set(author_list))


@router.get("/search/author-first-names")
async def get_author_first_names(db: Session = Depends(get_db)):
    """Distinct author first names (for typeahead, #92)."""
    rows = db.query(Book.author_name_first).filter(Book.author_name_first.isnot(None)).distinct().all()
    return sorted({r[0] for r in rows if r[0] and r[0].strip()})


@router.get("/search/author-last-names")
async def get_author_last_names(db: Session = Depends(get_db)):
    """Distinct author last names (for typeahead, #92)."""
    rows = db.query(Book.author_name_second).filter(Book.author_name_second.isnot(None)).distinct().all()
    return sorted({r[0] for r in rows if r[0] and r[0].strip()})


@router.get("/search/universes")
async def get_universes(db: Session = Depends(get_db)):
    """Distinct universes (for typeahead, #92)."""
    rows = db.query(Book.universe).filter(Book.universe.isnot(None)).distinct().all()
    return sorted({r[0] for r in rows if r[0] and r[0].strip()})


@router.get("/search/titles")
async def get_titles(db: Session = Depends(get_db)):
    """Distinct titles (for dup-awareness typeahead, #92)."""
    rows = db.query(Book.title).filter(Book.title.isnot(None)).distinct().all()
    return sorted({r[0] for r in rows if r[0] and r[0].strip()})


@router.get("/search/series")
async def get_series(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get list of all series."""
    series = db.query(Book.series).filter(Book.series.isnot(None)).distinct().all()
    return sorted([s[0] for s in series if s[0]])


@router.get("/search/genres")
async def get_genres(db: Session = Depends(get_db)):
    """Get list of all genres."""
    genres = db.query(Book.genre).filter(Book.genre.isnot(None)).distinct().all()
    return sorted([g[0] for g in genres if g[0]])


@router.get("/search/tags")
async def get_tags(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get list of all tags."""
    tags = db.query(Tag).order_by(Tag.name).all()
    return [tag.name for tag in tags]
