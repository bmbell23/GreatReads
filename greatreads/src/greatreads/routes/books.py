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
from ..models.user import User
from ..config import settings
from ..auth import get_current_user

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
    return db_book


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
    return db_book


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


@router.post("/bulk-update")
async def bulk_update_books(
    request: Request,
    payload: BulkUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Set shared field(s) across many books at once (#93). A field is applied only if
    it was sent (exclude_unset); send an empty string to deliberately clear a field."""
    fields = payload.model_dump(exclude_unset=True, exclude={'ids'})
    # treat "" as an explicit clear → None; absent fields were already dropped above
    fields = {k: (None if v == "" else v) for k, v in fields.items()}
    if not payload.ids or not fields:
        return {"updated": 0}
    books = db.query(Book).filter(Book.id.in_(payload.ids)).all()
    for b in books:
        for k, v in fields.items():
            setattr(b, k, v)
    db.commit()
    return {"updated": len(books), "fields": list(fields.keys())}


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
