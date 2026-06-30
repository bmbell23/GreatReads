"""News item model — a detected upcoming/new book release (#68 Phase A).

One row per *work* (editions collapsed) surfaced on the News page. Populated by
`services/news_service.poll_releases` from the Google Books API; never written by
the user except `seen`/`dismissed`. Owned books (already in the library) are
filtered out before insert, so this table only holds candidates the user doesn't
have yet.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, Text

from ..database import Base


class NewsItem(Base):
    """A detected new/upcoming release for a watched author."""

    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True)

    # Identity / dedupe. google_books_id is the representative edition's volume id
    # and is unique (upsert target on re-poll). work_key is the synthesized
    # normalized (title, author) used to collapse editions and to honor dismissals.
    google_books_id = Column(String, unique=True, nullable=False, index=True)
    work_key = Column(String, index=True)

    author_name = Column(String, nullable=False)   # the watched author this came from
    title = Column(String, nullable=False)
    subtitle = Column(String)

    # Pub date of the representative edition. Year-only/month-only dates are stored
    # as Jan-1 / 1st with date_precision recording the real granularity so the UI
    # can render "2026" vs "Oct 27, 2026".
    published_date = Column(Date)
    date_precision = Column(String)                 # 'day' | 'month' | 'year' | None

    isbn_13 = Column(String)
    isbn_10 = Column(String)
    thumbnail_url = Column(String)
    preview_link = Column(String)

    matched_series = Column(String)                 # best-effort series name (from our DB), or None
    series_number = Column(Float)                   # parsed book number within the series, or None
    genre = Column(String)                          # primary Google category, cleaned
    matched_book_id = Column(Integer)               # set when this matches a book already in the DB
    tracked = Column(Boolean, default=False, nullable=False)  # in the DB but no owned copy → "on your radar"
    category = Column(String, default="book", nullable=False)  # legacy display; superseded by flags
    is_comic = Column(Boolean, default=False, nullable=False)   # independent flags so a book can be
    is_reprint = Column(Boolean, default=False, nullable=False) # both (comic AND reprint) — AND filtering
    kind = Column(String, nullable=False)           # 'upcoming' | 'new'
    low_confidence = Column(Boolean, default=False, nullable=False)  # missing cover/isbn
    # Enrichment (#69 — OpenLibrary/Hardcover cross-reference)
    binding = Column(String)                        # 'Hardcover' | 'Paperback' | None
    first_publish_year = Column(Integer)            # work's original year (drives reprint flag)
    word_count = Column(Integer)                    # estimated from page_count (~300 wpp)
    raw_json = Column(Text)                          # full normalized API record, for offline re-parsing

    seen = Column(Boolean, default=False, nullable=False)
    dismissed = Column(Boolean, default=False, nullable=False)

    discovered_at = Column(DateTime, default=datetime.utcnow)
    last_polled_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "google_books_id": self.google_books_id,
            "author_name": self.author_name,
            "title": self.title,
            "subtitle": self.subtitle,
            "published_date": self.published_date.isoformat() if self.published_date else None,
            "date_precision": self.date_precision,
            "isbn_13": self.isbn_13,
            "isbn_10": self.isbn_10,
            "thumbnail_url": self.thumbnail_url,
            "preview_link": self.preview_link,
            "matched_series": self.matched_series,
            "series_number": self.series_number,
            "genre": self.genre,
            "matched_book_id": self.matched_book_id,
            "tracked": self.tracked,
            "category": self.category,
            "is_comic": self.is_comic,
            "is_reprint": self.is_reprint,
            "kind": self.kind,
            "low_confidence": self.low_confidence,
            "binding": self.binding,
            "first_publish_year": self.first_publish_year,
            "word_count": self.word_count,
            "seen": self.seen,
            "dismissed": self.dismissed,
        }
