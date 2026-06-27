"""Reading statistics API routes."""

from datetime import date
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import extract, and_, text

from ..database import get_db
from ..models.reading import Reading
from ..models.book import Book
from ..models.inventory import Inventory
from ..services.format_dominance import get_primary_format

router = APIRouter()


@router.get("/")
async def get_reading_stats(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get reading statistics with optional year/month filtering."""

    # Base query for finished readings with eager loading of book relationship
    query = db.query(Reading).options(joinedload(Reading.book)).filter(Reading.date_finished_actual.isnot(None))

    # Apply year filter
    if year:
        query = query.filter(extract('year', Reading.date_finished_actual) == year)

    # Apply month filter (only if year is also specified)
    if month and year:
        query = query.filter(extract('month', Reading.date_finished_actual) == month)

    # Get all finished readings for this period
    readings = query.all()

    # Books Read by Format
    format_counts = {
        "Audio": 0,
        "Ebook": 0,
        "Physical": 0
    }

    for reading in readings:
        if reading.media:
            media_normalized = reading.media.lower()
            if media_normalized in ['audio', 'audiobook']:
                format_counts["Audio"] += 1
            elif media_normalized in ['kindle', 'ebook']:
                format_counts["Ebook"] += 1
            elif media_normalized in ['physical', 'hardcover', 'paperback']:
                format_counts["Physical"] += 1

    # Reading Speed by Format (words per day)
    reading_speeds = {
        "Audio": {"total_words": 0, "total_days": 0, "count": 0},
        "Ebook": {"total_words": 0, "total_days": 0, "count": 0},
        "Physical": {"total_words": 0, "total_days": 0, "count": 0}
    }

    for reading in readings:
        if reading.media and reading.book and reading.book.word_count:
            # Calculate days elapsed
            if reading.date_started and reading.date_finished_actual:
                days_elapsed = (
                    reading.date_finished_actual - reading.date_started
                ).days
                if days_elapsed > 0:  # Avoid division by zero
                    media_normalized = reading.media.lower()
                    if media_normalized in ['audio', 'audiobook']:
                        reading_speeds["Audio"]["total_words"] += (
                            reading.book.word_count
                        )
                        reading_speeds["Audio"]["total_days"] += days_elapsed
                        reading_speeds["Audio"]["count"] += 1
                    elif media_normalized in ['kindle', 'ebook']:
                        reading_speeds["Ebook"]["total_words"] += (
                            reading.book.word_count
                        )
                        reading_speeds["Ebook"]["total_days"] += days_elapsed
                        reading_speeds["Ebook"]["count"] += 1
                    elif media_normalized in ['physical', 'hardcover', 'paperback']:
                        reading_speeds["Physical"]["total_words"] += (
                            reading.book.word_count
                        )
                        reading_speeds["Physical"]["total_days"] += days_elapsed
                        reading_speeds["Physical"]["count"] += 1

    # Calculate average words per day for each format
    avg_reading_speeds = {}
    for format_name, data in reading_speeds.items():
        if data["total_days"] > 0:
            avg_reading_speeds[format_name] = round(
                data["total_words"] / data["total_days"]
            )
        else:
            avg_reading_speeds[format_name] = 0

    # Books/Words/Pages Read by Author (top 10 authors + "Other") - broken down by media format
    author_books = {}
    author_words = {}
    author_pages = {}
    for reading in readings:
        if reading.book:
            author = reading.book.author
            if author:
                if author not in author_books:
                    author_books[author] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                    author_words[author] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                    author_pages[author] = {"Audio": 0, "Ebook": 0, "Physical": 0}

                # Categorize by media format
                if reading.media:
                    media_normalized = reading.media.lower()
                    format_key = None
                    if media_normalized in ['audio', 'audiobook']:
                        format_key = "Audio"
                    elif media_normalized in ['kindle', 'ebook']:
                        format_key = "Ebook"
                    elif media_normalized in ['physical', 'hardcover', 'paperback']:
                        format_key = "Physical"

                    if format_key:
                        author_books[author][format_key] += 1
                        if reading.book.word_count:
                            author_words[author][format_key] += reading.book.word_count
                        if reading.book.page_count:
                            author_pages[author][format_key] += reading.book.page_count

    # Sort by total word count and get top 7
    sorted_authors = sorted(
        author_books.items(),
        key=lambda x: author_words[x[0]]["Audio"] + author_words[x[0]]["Ebook"] + author_words[x[0]]["Physical"],
        reverse=True
    )
    top_authors = sorted_authors[:7]

    # (No "Other" category — just show the top 7 authors as-is)

    # Books/Words/Pages Read by Genre - broken down by media format
    genre_books = {}
    genre_words = {}
    genre_pages = {}
    for reading in readings:
        if reading.book and reading.book.genre:
            genre = reading.book.genre
            if genre not in genre_books:
                genre_books[genre] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                genre_words[genre] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                genre_pages[genre] = {"Audio": 0, "Ebook": 0, "Physical": 0}

            # Categorize by media format
            if reading.media:
                media_normalized = reading.media.lower()
                format_key = None
                if media_normalized in ['audio', 'audiobook']:
                    format_key = "Audio"
                elif media_normalized in ['kindle', 'ebook']:
                    format_key = "Ebook"
                elif media_normalized in ['physical', 'hardcover', 'paperback']:
                    format_key = "Physical"

                if format_key:
                    genre_books[genre][format_key] += 1
                    if reading.book.word_count:
                        genre_words[genre][format_key] += reading.book.word_count
                    if reading.book.page_count:
                        genre_pages[genre][format_key] += reading.book.page_count

    # Sort genres by total book count
    sorted_genres = sorted(
        genre_books.items(),
        key=lambda x: x[1]["Audio"] + x[1]["Ebook"] + x[1]["Physical"],
        reverse=True
    )

    # Books/Words/Pages Read by Decade Published - broken down by media format
    decade_books = {}
    decade_words = {}
    decade_pages = {}
    for reading in readings:
        if reading.book and reading.book.year_published:
            # Calculate decade (e.g., 1990 -> "1990s", 2000 -> "2000s")
            decade = (reading.book.year_published // 10) * 10
            decade_label = f"{decade}s"
            if decade_label not in decade_books:
                decade_books[decade_label] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                decade_words[decade_label] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                decade_pages[decade_label] = {"Audio": 0, "Ebook": 0, "Physical": 0}

            # Categorize by media format
            if reading.media:
                media_normalized = reading.media.lower()
                format_key = None
                if media_normalized in ['audio', 'audiobook']:
                    format_key = "Audio"
                elif media_normalized in ['kindle', 'ebook']:
                    format_key = "Ebook"
                elif media_normalized in ['physical', 'hardcover', 'paperback']:
                    format_key = "Physical"

                if format_key:
                    decade_books[decade_label][format_key] += 1
                    if reading.book.word_count:
                        decade_words[decade_label][format_key] += reading.book.word_count
                    if reading.book.page_count:
                        decade_pages[decade_label][format_key] += reading.book.page_count

    # Sort by decade (extract numeric value for proper chronological sorting)
    sorted_decades = sorted(
        decade_books.items(),
        key=lambda x: int(x[0].replace('s', ''))
    )

    # Word Count Distribution - broken down by media format
    word_count_ranges = {
        "500k+": {"Audio": 0, "Ebook": 0, "Physical": 0},
        "400-500k": {"Audio": 0, "Ebook": 0, "Physical": 0},
        "300-400k": {"Audio": 0, "Ebook": 0, "Physical": 0},
        "200-300k": {"Audio": 0, "Ebook": 0, "Physical": 0},
        "100-200k": {"Audio": 0, "Ebook": 0, "Physical": 0},
        "50-100k": {"Audio": 0, "Ebook": 0, "Physical": 0},
        "<50k": {"Audio": 0, "Ebook": 0, "Physical": 0}
    }

    for reading in readings:
        if reading.book and reading.book.word_count:
            wc = reading.book.word_count

            # Determine range
            if wc >= 500000:
                range_key = "500k+"
            elif wc >= 400000:
                range_key = "400-500k"
            elif wc >= 300000:
                range_key = "300-400k"
            elif wc >= 200000:
                range_key = "200-300k"
            elif wc >= 100000:
                range_key = "100-200k"
            elif wc >= 50000:
                range_key = "50-100k"
            else:
                range_key = "<50k"

            # Categorize by media format
            if reading.media:
                media_normalized = reading.media.lower()
                format_key = None
                if media_normalized in ['audio', 'audiobook']:
                    format_key = "Audio"
                elif media_normalized in ['kindle', 'ebook']:
                    format_key = "Ebook"
                elif media_normalized in ['physical', 'hardcover', 'paperback']:
                    format_key = "Physical"

                if format_key:
                    word_count_ranges[range_key][format_key] += 1

    # Series Progress - all series that have been started
    # Get all books in series where at least one book has been read
    series_with_reads = db.query(Book.series).join(Reading).filter(
        and_(
            Book.series.isnot(None),
            Reading.date_finished_actual.isnot(None)
        )
    ).distinct().all()

    series_data = []
    for (series_name,) in series_with_reads:
        if not series_name:
            continue

        # Get all books in this series (exclude unpublished books)
        from datetime import date as date_type
        today = date_type.today()
        series_books = db.query(Book).filter(
            and_(
                Book.series == series_name,
                Book.date_published.isnot(None),
                Book.date_published <= today
            )
        ).order_by(Book.series_number).all()

        # Get universe from first book in series (all books in a series should have same universe)
        universe = series_books[0].universe if series_books else None

        # Track read and unread books by format (word counts and book counts)
        audio_words = 0
        ebook_words = 0
        physical_words = 0
        unread_large_words = 0  # >= 40k words
        unread_small_words = 0  # < 40k words

        audio_count = 0
        ebook_count = 0
        physical_count = 0
        unread_large_count = 0
        unread_small_count = 0

        # Track book titles for each category
        audio_titles = []
        ebook_titles = []
        physical_titles = []
        unread_large_titles = []
        unread_small_titles = []

        total_books = len(series_books)
        books_read = 0

        for book in series_books:
            # Check if this book has been read (get all readings for priority)
            book_readings = db.query(Reading).filter(
                and_(
                    Reading.book_id == book.id,
                    Reading.date_finished_actual.isnot(None)
                )
            ).all()

            word_count = book.word_count or 0

            if book_readings:
                books_read += 1
                # Book has been read - determine highest priority format
                # Priority: Physical > Ebook > Audio
                has_physical = False
                has_ebook = False
                has_audio = False

                for book_reading in book_readings:
                    if book_reading.media:
                        media_normalized = book_reading.media.lower()
                        if media_normalized in ['physical', 'hardcover', 'paperback']:
                            has_physical = True
                        elif media_normalized in ['kindle', 'ebook']:
                            has_ebook = True
                        elif media_normalized in ['audio', 'audiobook']:
                            has_audio = True

                # Assign to highest priority format
                if has_physical:
                    physical_words += word_count
                    physical_count += 1
                    physical_titles.append(book.title)
                elif has_ebook:
                    ebook_words += word_count
                    ebook_count += 1
                    ebook_titles.append(book.title)
                elif has_audio:
                    audio_words += word_count
                    audio_count += 1
                    audio_titles.append(book.title)
            else:
                # Book has not been read - categorize by word count
                if word_count >= 40000:
                    unread_large_words += word_count
                    unread_large_count += 1
                    unread_large_titles.append(book.title)
                else:
                    unread_small_words += word_count
                    unread_small_count += 1
                    unread_small_titles.append(book.title)

        total_words = audio_words + ebook_words + physical_words + unread_large_words + unread_small_words
        is_completed = books_read == total_books

        series_data.append({
            "series": series_name,
            "universe": universe,
            "total": total_books,
            "total_words": total_words,
            "Audio": audio_words,
            "Ebook": ebook_words,
            "Physical": physical_words,
            "Unread_Large": unread_large_words,
            "Unread_Small": unread_small_words,
            "Audio_count": audio_count,
            "Ebook_count": ebook_count,
            "Physical_count": physical_count,
            "Unread_Large_count": unread_large_count,
            "Unread_Small_count": unread_small_count,
            "Audio_titles": audio_titles,
            "Ebook_titles": ebook_titles,
            "Physical_titles": physical_titles,
            "Unread_Large_titles": unread_large_titles,
            "Unread_Small_titles": unread_small_titles,
            "completed": is_completed
        })

    # Sort by total word count (longest series first)
    series_data.sort(key=lambda x: x["total_words"], reverse=True)

    # Gender of Authors Read
    gender_counts = {"Male": 0, "Female": 0}
    for reading in readings:
        if reading.book and reading.book.author_gender:
            gender_normalized = reading.book.author_gender.strip().lower()
            if gender_normalized in ['male', 'm']:
                gender_counts["Male"] += 1
            elif gender_normalized in ['female', 'f']:
                gender_counts["Female"] += 1

    # Days Read After Publication
    days_after_pub_ranges = {
        "<1 year": 0,
        "1-5 years": 0,
        "5-20 years": 0,
        "20-40 years": 0,
        "40+ years": 0
    }

    for reading in readings:
        if reading.book and reading.book.date_published and reading.date_finished_actual:
            days_diff = (reading.date_finished_actual - reading.book.date_published).days
            years_diff = days_diff / 365.25

            if years_diff < 1:
                days_after_pub_ranges["<1 year"] += 1
            elif years_diff < 5:
                days_after_pub_ranges["1-5 years"] += 1
            elif years_diff < 20:
                days_after_pub_ranges["5-20 years"] += 1
            elif years_diff < 40:
                days_after_pub_ranges["20-40 years"] += 1
            else:
                days_after_pub_ranges["40+ years"] += 1

    # Days to Finish Books Read
    days_to_finish_ranges = {
        "1-2 days": 0,
        "3-4 days": 0,
        "5-7 days": 0,
        "8-14 days": 0,
        "15-30 days": 0,
        "31+ days": 0
    }

    for reading in readings:
        if reading.date_started and reading.date_finished_actual:
            days_to_finish = (reading.date_finished_actual - reading.date_started).days

            if days_to_finish <= 2:
                days_to_finish_ranges["1-2 days"] += 1
            elif days_to_finish <= 4:
                days_to_finish_ranges["3-4 days"] += 1
            elif days_to_finish <= 7:
                days_to_finish_ranges["5-7 days"] += 1
            elif days_to_finish <= 14:
                days_to_finish_ranges["8-14 days"] += 1
            elif days_to_finish <= 30:
                days_to_finish_ranges["15-30 days"] += 1
            else:
                days_to_finish_ranges["31+ days"] += 1

    # Get available years and months for filtering
    all_readings = db.query(Reading).filter(
        Reading.date_finished_actual.isnot(None)
    ).all()
    available_years = sorted(
        set(
            r.date_finished_actual.year
            for r in all_readings
            if r.date_finished_actual
        ),
        reverse=True
    )

    available_months = []
    if year:
        year_readings = [
            r for r in all_readings
            if r.date_finished_actual and r.date_finished_actual.year == year
        ]
        available_months = sorted(
            set(
                r.date_finished_actual.month
                for r in year_readings
                if r.date_finished_actual
            )
        )

    # Books/Words/Pages by Year (for "All Years" view) - broken down by media format
    books_by_year = {}
    words_by_year = {}
    pages_by_year = {}
    if not year:  # Only calculate when viewing all years
        for reading in all_readings:
            if reading.date_finished_actual:
                yr = reading.date_finished_actual.year
                if yr not in books_by_year:
                    books_by_year[yr] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                    words_by_year[yr] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                    pages_by_year[yr] = {"Audio": 0, "Ebook": 0, "Physical": 0}

                # Categorize by media format
                if reading.media:
                    media_normalized = reading.media.lower()
                    format_key = None
                    if media_normalized in ['audio', 'audiobook']:
                        format_key = "Audio"
                    elif media_normalized in ['kindle', 'ebook']:
                        format_key = "Ebook"
                    elif media_normalized in ['physical', 'hardcover', 'paperback']:
                        format_key = "Physical"

                    if format_key:
                        books_by_year[yr][format_key] += 1
                        if reading.book:
                            if reading.book.word_count:
                                words_by_year[yr][format_key] += reading.book.word_count
                            if reading.book.page_count:
                                pages_by_year[yr][format_key] += reading.book.page_count

        # Sort by year
        books_by_year = dict(sorted(books_by_year.items()))
        words_by_year = dict(sorted(words_by_year.items()))
        pages_by_year = dict(sorted(pages_by_year.items()))

    # Books/Words/Pages by Month (for specific year view) - broken down by media format
    books_by_month = {}
    words_by_month = {}
    pages_by_month = {}
    if year and not month:  # Only calculate when viewing a specific year (all months)
        year_readings = [
            r for r in all_readings
            if r.date_finished_actual and r.date_finished_actual.year == year
        ]
        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        for reading in year_readings:
            if reading.date_finished_actual:
                mo = reading.date_finished_actual.month
                month_label = month_names[mo - 1]
                if month_label not in books_by_month:
                    books_by_month[month_label] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                    words_by_month[month_label] = {"Audio": 0, "Ebook": 0, "Physical": 0}
                    pages_by_month[month_label] = {"Audio": 0, "Ebook": 0, "Physical": 0}

                # Categorize by media format
                if reading.media:
                    media_normalized = reading.media.lower()
                    format_key = None
                    if media_normalized in ['audio', 'audiobook']:
                        format_key = "Audio"
                    elif media_normalized in ['kindle', 'ebook']:
                        format_key = "Ebook"
                    elif media_normalized in ['physical', 'hardcover', 'paperback']:
                        format_key = "Physical"

                    if format_key:
                        books_by_month[month_label][format_key] += 1
                        if reading.book:
                            if reading.book.word_count:
                                words_by_month[month_label][format_key] += reading.book.word_count
                            if reading.book.page_count:
                                pages_by_month[month_label][format_key] += reading.book.page_count

        # Sort by month order
        sorted_books = {
            month: books_by_month.get(month, {"Audio": 0, "Ebook": 0, "Physical": 0})
            for month in month_names
            if month in books_by_month
        }
        sorted_words = {
            month: words_by_month.get(month, {"Audio": 0, "Ebook": 0, "Physical": 0})
            for month in month_names
            if month in words_by_month
        }
        sorted_pages = {
            month: pages_by_month.get(month, {"Audio": 0, "Ebook": 0, "Physical": 0})
            for month in month_names
            if month in pages_by_month
        }
        books_by_month = sorted_books
        words_by_month = sorted_words
        pages_by_month = sorted_pages

    return {
        "books_by_format": format_counts,
        "reading_speed": avg_reading_speeds,
        "top_authors": {
            "books": [
                {
                    "author": author,
                    "Audio": author_books[author]["Audio"],
                    "Ebook": author_books[author]["Ebook"],
                    "Physical": author_books[author]["Physical"]
                }
                for author, _ in top_authors
            ],
            "words": [
                {
                    "author": author,
                    "Audio": author_words[author]["Audio"],
                    "Ebook": author_words[author]["Ebook"],
                    "Physical": author_words[author]["Physical"]
                }
                for author, _ in top_authors
            ],
            "pages": [
                {
                    "author": author,
                    "Audio": author_pages[author]["Audio"],
                    "Ebook": author_pages[author]["Ebook"],
                    "Physical": author_pages[author]["Physical"]
                }
                for author, _ in top_authors
            ]
        },
        "books_by_genre": {
            "books": [
                {
                    "genre": genre,
                    "Audio": stats["Audio"],
                    "Ebook": stats["Ebook"],
                    "Physical": stats["Physical"]
                }
                for genre, stats in sorted_genres
            ],
            "words": [
                {
                    "genre": genre,
                    "Audio": genre_words[genre]["Audio"],
                    "Ebook": genre_words[genre]["Ebook"],
                    "Physical": genre_words[genre]["Physical"]
                }
                for genre, _ in sorted_genres
            ],
            "pages": [
                {
                    "genre": genre,
                    "Audio": genre_pages[genre]["Audio"],
                    "Ebook": genre_pages[genre]["Ebook"],
                    "Physical": genre_pages[genre]["Physical"]
                }
                for genre, _ in sorted_genres
            ]
        },
        "books_by_decade": {
            "books": [
                {
                    "decade": decade,
                    "Audio": stats["Audio"],
                    "Ebook": stats["Ebook"],
                    "Physical": stats["Physical"]
                }
                for decade, stats in sorted_decades
            ],
            "words": [
                {
                    "decade": decade,
                    "Audio": decade_words[decade]["Audio"],
                    "Ebook": decade_words[decade]["Ebook"],
                    "Physical": decade_words[decade]["Physical"]
                }
                for decade, _ in sorted_decades
            ],
            "pages": [
                {
                    "decade": decade,
                    "Audio": decade_pages[decade]["Audio"],
                    "Ebook": decade_pages[decade]["Ebook"],
                    "Physical": decade_pages[decade]["Physical"]
                }
                for decade, _ in sorted_decades
            ]
        },
        "books_by_year": books_by_year,
        "words_by_year": words_by_year,
        "pages_by_year": pages_by_year,
        "books_by_month": books_by_month,
        "words_by_month": words_by_month,
        "pages_by_month": pages_by_month,
        "word_count_distribution": word_count_ranges,
        "library_word_counts": _calc_library_word_counts(db),
        "series_progress": series_data,
        "author_gender": gender_counts,
        "days_after_publication": days_after_pub_ranges,
        "days_to_finish": days_to_finish_ranges,
        "filters": {
            "available_years": available_years,
            "available_months": available_months,
            "selected_year": year,
            "selected_month": month
        },
        "total_books_read": len(readings)
    }


def _calc_library_word_counts(db: Session) -> dict:
    """Calculate read/unread word counts for owned books, grouped by format."""
    # All inventory entries joined with their book
    inventories = db.query(Inventory).join(Book, Inventory.book_id == Book.id).all()

    # Build a set of book_ids that have been finished at least once
    finished_ids = {
        r.book_id
        for r in db.query(Reading.book_id).filter(Reading.date_finished_actual.isnot(None)).all()
    }

    result = {
        "Audio":    {"read": 0, "unread": 0},
        "Ebook":    {"read": 0, "unread": 0},
        "Physical": {"read": 0, "unread": 0},
        "Overall":  {"read": 0, "unread": 0},
    }

    for inv in inventories:
        wc = inv.book.word_count or 0
        if wc == 0:
            continue
        is_read = inv.book_id in finished_ids
        key = "read" if is_read else "unread"

        formats_owned = []
        # Skip GA books flagged as not-in-library (title match + author mismatch).
        if inv.owned_audio and (not inv.graphic_audio or inv.owned_in_library):
            formats_owned.append("Audio")
        if inv.owned_ebook:
            formats_owned.append("Ebook")
        if inv.owned_physical:
            formats_owned.append("Physical")

        added_to_overall = False
        for fmt in formats_owned:
            result[fmt][key] += wc
            if not added_to_overall:
                result["Overall"][key] += wc
                added_to_overall = True

    return result


# ---------- Reading-activity analytics (#30) ----------
# Sourced from the reading_activity time-series (written by the ereader app on
# every progress save). Returns empty until reads accrue — there is no backfill.

def _wpm_from(mpw_sum, mpw_n, minutes, words):
    """Measured WPM (from real ms-per-word samples) when available, else derive
    words/minute from the day's totals. Returns None if neither is meaningful."""
    if mpw_n and mpw_sum and mpw_sum > 0:
        mpw = mpw_sum / mpw_n
        if mpw > 0:
            return round(60000.0 / mpw)
    if minutes and minutes > 0 and words and words > 0:
        return round(words / minutes)
    return None


@router.get("/activity")
async def get_reading_activity(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Daily reading time / words / measured WPM per format (#30 phase 2)."""
    daily_minutes: Dict[str, Dict[str, float]] = {}
    daily_words: Dict[str, Dict[str, int]] = {}
    daily_wpm: Dict[str, float] = {}              # ebook measured WPM by date
    totals: Dict[str, Dict[str, Any]] = {"minutes": {}, "words": {}}
    try:
        rows = db.execute(text(
            "SELECT activity_date, format, SUM(minutes) AS minutes, SUM(words) AS words, "
            "SUM(wpm_mpw_sum) AS mpw_sum, SUM(wpm_n) AS mpw_n "
            "FROM reading_activity GROUP BY activity_date, format ORDER BY activity_date"
        )).fetchall()
    except Exception:
        rows = []   # table not created yet (nothing logged)
    for d, fmt, minutes, words, mpw_sum, mpw_n in rows:
        minutes = round(float(minutes or 0), 1)
        words = int(words or 0)
        daily_minutes.setdefault(fmt, {})[d] = minutes
        daily_words.setdefault(fmt, {})[d] = words
        totals["minutes"][fmt] = round(totals["minutes"].get(fmt, 0) + minutes, 1)
        totals["words"][fmt] = totals["words"].get(fmt, 0) + words
        if fmt == "Ebook":
            w = _wpm_from(mpw_sum, mpw_n, minutes, words)
            if w:
                daily_wpm[d] = w
    return {"daily_minutes": daily_minutes, "daily_words": daily_words,
            "daily_wpm": daily_wpm, "totals": totals}


@router.get("/book-time/{book_id}")
async def get_book_reading_time(book_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Per-book real reading/listening time (#30), for the card popups. Resolves the
    book's Calibre/ABS external ids and sums reading_activity across formats."""
    formats: Dict[str, Any] = {}
    total_minutes, total_words = 0.0, 0
    try:
        rows = db.execute(text(
            "SELECT ra.format, SUM(ra.minutes) AS minutes, SUM(ra.words) AS words, "
            "SUM(ra.wpm_mpw_sum) AS mpw_sum, SUM(ra.wpm_n) AS mpw_n "
            "FROM reading_activity ra WHERE ra.book_key IN ("
            "  SELECT CASE WHEN ei.source='audiobookshelf' THEN 'abs:' || ei.external_id "
            "              ELSE ei.external_id END "
            "  FROM external_imports ei WHERE ei.book_id = :bid "
            "  UNION "
            "  SELECT 'phys:' || r.id FROM read r WHERE r.book_id = :bid) "
            "GROUP BY ra.format"
        ), {"bid": book_id}).fetchall()
    except Exception:
        rows = []
    for fmt, minutes, words, mpw_sum, mpw_n in rows:
        minutes = round(float(minutes or 0), 1)
        words = int(words or 0)
        entry: Dict[str, Any] = {"minutes": minutes, "words": words}
        w = _wpm_from(mpw_sum, mpw_n, minutes, words)
        if w:
            entry["wpm"] = w
        formats[fmt] = entry
        total_minutes += minutes
        total_words += words
    return {"total_minutes": round(total_minutes, 1), "total_words": total_words,
            "formats": formats,
            # Derived dominant format from cumulative word-equivalents (#67 phase 1).
            "primary_format": get_primary_format(db, book_id)}


@router.get("/home-momentum")
async def get_home_momentum(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Lightweight 'this year' momentum for the Home page (#65): books finished
    year-to-date + words read year-to-date (from the reading-activity rollup)."""
    year = date.today().year
    books_this_year = db.query(Reading).filter(
        Reading.date_finished_actual.isnot(None),
        extract('year', Reading.date_finished_actual) == year,
    ).count()
    try:
        row = db.execute(text(
            "SELECT COALESCE(SUM(words), 0) FROM reading_activity "
            "WHERE substr(activity_date, 1, 4) = :yr"
        ), {"yr": str(year)}).fetchone()
        words_this_year = int(row[0] or 0)
    except Exception:
        words_this_year = 0
    return {"year": year, "books_this_year": books_this_year,
            "words_this_year": words_this_year}
