"""Library browsing and management API routes."""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from ..database import get_db
from ..models.book import Book, BookResponse
from ..models.reading import Reading
from ..models.inventory import Inventory
from ._book_enrich import enrich_book_dict

router = APIRouter()


@router.get("/books", response_model=List[Dict[str, Any]])
async def get_library_books(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    search: Optional[str] = Query(None),
    author: Optional[str] = Query(None),
    series: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    read_status: Optional[str] = Query(None),  # "read", "unread", "in_progress"
    media_owned: Optional[str] = Query(None),  # "ebook", "physical", "audio"
    sort_by: str = Query("title"),  # "title", "author", "date_published", "series"
    sort_order: str = Query("asc"),  # "asc", "desc"
    db: Session = Depends(get_db)
):
    """Get library books with comprehensive filtering and sorting."""

    # Base query with joins - only include books that are owned
    query = db.query(Book).outerjoin(Reading).join(Inventory).filter(
        or_(
            Inventory.owned_physical == True,
            Inventory.owned_ebook == True,
            Inventory.owned_audio == True
        )
    )

    # Apply filters
    if search:
        query = query.filter(Book.title.ilike(f"%{search}%"))

    if author:
        query = query.filter(
            or_(
                Book.author_name_first.ilike(f"%{author}%"),
                Book.author_name_second.ilike(f"%{author}%")
            )
        )

    if series:
        query = query.filter(Book.series.ilike(f"%{series}%"))

    if genre:
        query = query.filter(Book.genre.ilike(f"%{genre}%"))

    if read_status:
        if read_status == "read":
            query = query.filter(Reading.date_finished_actual.isnot(None))
        elif read_status == "unread":
            query = query.filter(
                or_(
                    Reading.id.is_(None),
                    Reading.date_finished_actual.is_(None)
                )
            )
        elif read_status == "in_progress":
            query = query.filter(
                and_(
                    Reading.date_started.isnot(None),
                    Reading.date_finished_actual.is_(None)
                )
            )

    if media_owned:
        if media_owned.lower() == "audio":
            query = query.filter(Inventory.owned_audio == True)
        elif media_owned.lower() == "ebook":
            query = query.filter(Inventory.owned_ebook == True)
        elif media_owned.lower() == "physical":
            query = query.filter(Inventory.owned_physical == True)

    # Apply sorting
    if sort_by == "author":
        if sort_order == "desc":
            query = query.order_by(Book.author_name_second.desc(), Book.author_name_first.desc())
        else:
            query = query.order_by(Book.author_name_second.asc(), Book.author_name_first.asc())
    elif sort_by == "date_published":
        if sort_order == "desc":
            query = query.order_by(Book.date_published.desc())
        else:
            query = query.order_by(Book.date_published.asc())
    elif sort_by == "series":
        if sort_order == "desc":
            query = query.order_by(Book.series.desc(), Book.series_number.desc())
        else:
            query = query.order_by(Book.series.asc(), Book.series_number.asc())
    else:  # title
        if sort_order == "desc":
            query = query.order_by(Book.title.desc())
        else:
            query = query.order_by(Book.title.asc())

    # Get distinct books (in case of multiple readings/inventory entries)
    books = query.distinct(Book.id).offset(skip).limit(limit).all()

    # Enrich with additional data
    enriched_books = []
    for book in books:
        book_data = book.to_dict()

        # Add reading information
        readings = db.query(Reading).filter(Reading.book_id == book.id).all()
        book_data["readings"] = [r.to_dict() for r in readings]
        book_data["read_count"] = len([r for r in readings if r.date_finished_actual])
        book_data["is_read"] = any(r.date_finished_actual for r in readings)
        book_data["is_in_progress"] = any(
            r.date_started and not r.date_finished_actual for r in readings
        )

        # Inventory + owned-media + external source links (Calibre / Audiobookshelf).
        # The unified home/popup uses these to open a readable book and show shelf
        # location; books with neither link are tracking-only (physical / manual).
        enrich_book_dict(book_data, book.id, db)

        enriched_books.append(book_data)

    return enriched_books


@router.get("/stats")
async def get_library_stats(db: Session = Depends(get_db)):
    """Get library statistics."""

    # Total books (only owned)
    total_books = db.query(Book).join(Inventory).filter(
        or_(
            Inventory.owned_physical == True,
            Inventory.owned_ebook == True,
            Inventory.owned_audio == True
        )
    ).count()

    # Total reading sessions (includes re-reads)
    total_readings = db.query(Reading).filter(
        Reading.date_finished_actual.isnot(None)
    ).count()

    # Unique books read (all books, not just owned)
    total_read_books = db.query(Book).join(Reading).filter(
        Reading.date_finished_actual.isnot(None)
    ).distinct().count()

    # Books read that you own - for library page
    read_books = db.query(Book).join(Reading).join(Inventory).filter(
        and_(
            Reading.date_finished_actual.isnot(None),
            or_(
                Inventory.owned_physical == True,
                Inventory.owned_ebook == True,
                Inventory.owned_audio == True
            )
        )
    ).distinct().count()

    # Count books that have never been read (no reading entries with finished date)
    # This correctly handles rereads - a book being reread is still "read", not "unread"
    books_with_finished_reads = db.query(Book.id).join(Reading).join(Inventory).filter(
        and_(
            Reading.date_finished_actual.isnot(None),
            or_(
                Inventory.owned_physical == True,
                Inventory.owned_ebook == True,
                Inventory.owned_audio == True
            )
        )
    ).distinct().subquery()

    # Owned books that haven't been read (Owned TBR)
    unread_books = db.query(Book).join(Inventory).filter(
        and_(
            or_(
                Inventory.owned_physical == True,
                Inventory.owned_ebook == True,
                Inventory.owned_audio == True
            ),
            ~Book.id.in_(books_with_finished_reads)
        )
    ).count()

    # General TBR count (all unfinished readings)
    general_tbr = db.query(Reading).filter(
        Reading.date_finished_actual.is_(None)
    ).count()

    # Per-format Library TBR (#65 Home stats): owned in that format AND never
    # finished. Library-derived (ownership), distinct from the reading-plan TBR.
    ebook_tbr = db.query(Book.id).join(Inventory).filter(
        and_(Inventory.owned_ebook == True, ~Book.id.in_(books_with_finished_reads))
    ).distinct().count()
    audio_tbr = db.query(Book.id).join(Inventory).filter(
        and_(
            Inventory.owned_audio == True,
            (Inventory.graphic_audio == False) | (Inventory.owned_in_library == True),
            ~Book.id.in_(books_with_finished_reads),
        )
    ).distinct().count()
    physical_tbr = db.query(Book.id).join(Inventory).filter(
        and_(Inventory.owned_physical == True, ~Book.id.in_(books_with_finished_reads))
    ).distinct().count()

    # Books by media owned
    # Exclude Graphic Audio books flagged as not-in-library (title match + author mismatch).
    audio_count = db.query(Inventory.book_id).filter(
        Inventory.owned_audio == True,
        (Inventory.graphic_audio == False) | (Inventory.owned_in_library == True),
    ).distinct().count()
    ebook_count = db.query(Inventory.book_id).filter(Inventory.owned_ebook == True).distinct().count()
    physical_count = db.query(Inventory.book_id).filter(Inventory.owned_physical == True).distinct().count()

    media_stats = {
        "Audio": audio_count,
        "Ebook": ebook_count,
        "Physical": physical_count
    }

    # Genre distribution
    genre_stats = db.query(
        Book.genre,
        func.count(Book.id).label('count')
    ).filter(Book.genre.isnot(None)).group_by(Book.genre).all()

    # Author count
    author_count = db.query(
        Book.author_name_first,
        Book.author_name_second
    ).distinct().count()

    # Series count
    series_count = db.query(Book.series).filter(
        Book.series.isnot(None)
    ).distinct().count()

    return {
        "total_books": total_books,  # Books owned
        "total_readings": total_readings,  # Total reading sessions (includes re-reads)
        "total_read_books": total_read_books,  # Unique books read (for home page)
        "read_books": read_books,  # Books read that you own (for library page)
        "unread_books": unread_books,  # Owned books not read (Owned TBR)
        "general_tbr": general_tbr,  # All unfinished readings (General TBR)
        "ebook_tbr": ebook_tbr,  # Owned-unread ebooks (Library, per format)
        "audio_tbr": audio_tbr,  # Owned-unread audiobooks
        "physical_tbr": physical_tbr,  # Owned-unread physical
        "media_owned": media_stats,
        "genres": {genre: count for genre, count in genre_stats},
        "total_authors": author_count,
        "total_series": series_count,
    }


@router.get("/filters")
async def get_filter_options(db: Session = Depends(get_db)):
    """Get available filter options for the library."""

    # Authors
    authors = db.query(Book.author_name_first, Book.author_name_second).distinct().all()
    author_list = []
    for first, last in authors:
        if first or last:
            name_parts = [first, last]
            full_name = " ".join(filter(None, name_parts))
            author_list.append(full_name)

    # Series
    series = db.query(Book.series).filter(Book.series.isnot(None)).distinct().all()
    series_list = [s[0] for s in series if s[0]]

    # Genres
    genres = db.query(Book.genre).filter(Book.genre.isnot(None)).distinct().all()
    genre_list = [g[0] for g in genres if g[0]]

    # Media types (hardcoded since they're boolean columns)
    media_list = ["Audio", "Ebook", "Physical"]

    return {
        "authors": sorted(set(author_list)),
        "series": sorted(series_list),
        "genres": sorted(genre_list),
        "media_types": sorted(media_list),
        "read_statuses": ["read", "unread", "in_progress"],
        "sort_options": ["title", "author", "date_published", "series"],
    }
