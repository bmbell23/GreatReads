"""Reading model."""

import math
from datetime import date, datetime, timedelta
from typing import Optional, TYPE_CHECKING
from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Boolean, Float
from sqlalchemy.orm import relationship
from pydantic import BaseModel

from ..database import Base

if TYPE_CHECKING:
    from .book import BookResponse

# Reading speeds (words per day) by media type
READING_SPEEDS = {
    "ebook": 15000,
    "physical": 12000,
    "hardcover": 12000,
    "audio": 25000,
    "audiobook": 25000,
}
DEFAULT_WPD = 12000


class Reading(Base):
    """Reading session database model."""

    __tablename__ = 'read'

    id = Column(Integer, primary_key=True)
    id_previous = Column(Integer, ForeignKey('read.id'))
    book_id = Column(Integer, ForeignKey('books.id'), nullable=False)
    media = Column(String)
    date_started = Column(Date)
    date_finished_actual = Column(Date)
    date_paused = Column(Date)  # When set, progress calculation freezes
    rating_horror = Column(Float)
    rating_spice = Column(Float)
    rating_world_building = Column(Float)
    rating_writing = Column(Float)
    rating_characters = Column(Float)
    rating_readability = Column(Float)
    rating_enjoyment = Column(Float)
    rank = Column(Integer)
    days_estimate = Column(Float)  # Changed to Float to support fractional days for precise progress
    days_elapsed_to_read = Column(Integer)
    days_to_read_delta_from_estimate = Column(Integer)
    date_est_start = Column(Date)
    date_est_end = Column(Date)
    reread = Column(Boolean, default=False)
    days_estimate_override = Column(Boolean, default=False)
    current_percent = Column(Float)  # Current progress percentage (0-100) for IP books
    current_percent_manual_override = Column(Boolean, default=False)  # True if user manually set current_percent
    date_progress_set = Column(DateTime)  # DateTime when current_percent was manually set

    # Relationships
    book = relationship("Book", back_populates="readings")
    previous_reading = relationship("Reading", remote_side=[id], backref="subsequent_readings")

    @property
    def calculated_days_estimate(self) -> Optional[int]:
        """Calculate estimated days to read based on book word count and media type."""
        if not self.book or not self.book.word_count or not self.media:
            return None

        media_lower = self.media.lower()
        words_per_day = READING_SPEEDS.get(media_lower, DEFAULT_WPD)
        return math.ceil(self.book.word_count / words_per_day)

    @property
    def effective_days_estimate(self) -> Optional[int]:
        """Get the effective days estimate (from database or calculated).

        The days_estimate field in the database is updated by the chain calculator
        using the user's reading speed settings. We should use that value unless
        it's None, in which case we fall back to the calculated value.
        """
        # Use the database value if it exists (it's updated by chain calculator with user settings)
        if self.days_estimate is not None:
            return self.days_estimate
        # Fall back to calculated value if database value is None
        return self.calculated_days_estimate

    @property
    def date_finished_estimate(self) -> Optional[date]:
        """Calculate estimated finish date based on start date and days estimate."""
        if not self.date_started or not self.effective_days_estimate:
            return None
        return self.date_started + timedelta(days=self.effective_days_estimate)

    @property
    def actual_days_elapsed(self) -> Optional[int]:
        """Calculate actual days taken to read (inclusive)."""
        if not self.date_started or not self.date_finished_actual:
            return None
        # Add 1 to make it inclusive (Nov 1 to Nov 3 = 3 days, not 2)
        return (self.date_finished_actual - self.date_started).days + 1

    @property
    def days_delta_from_estimate(self) -> Optional[int]:
        """Calculate difference between estimated and actual days to read."""
        if not self.effective_days_estimate or not self.actual_days_elapsed:
            return None
        return self.actual_days_elapsed - self.effective_days_estimate

    @property
    def is_finished(self) -> bool:
        """Check if reading is finished."""
        return self.date_finished_actual is not None

    @property
    def is_started(self) -> bool:
        """Check if reading is started."""
        return self.date_started is not None

    @property
    def is_paused(self) -> bool:
        """Check if reading is paused."""
        return self.date_paused is not None

    @property
    def status(self) -> str:
        """Get reading status."""
        if self.is_finished:
            return "finished"
        elif self.is_paused:
            return "paused"
        elif self.is_started:
            return "in_progress"
        else:
            return "not_started"

    @property
    def current_progress_percent(self) -> Optional[float]:
        """Calculate current progress percentage for IP books.

        Returns the percentage of the book completed (0-100).

        If paused, returns the current_percent (frozen progress).
        If manual progress was set, calculates additional progress from that point
        based on WPD. Otherwise, calculates based on WPD from the start date.
        """
        if not self.is_started or self.is_finished:
            return None

        if not self.date_started or not self.book or not self.book.word_count or not self.media:
            return None

        # Progress is now DIRECTLY TRACKED: the Ereader writes the real reading
        # position into current_percent on every read, and the in-app "update
        # progress" control sets it too. So whenever we have an actual value, return
        # it as-is — no daily WPD projection. We used to project a moving "daily
        # goal" forward because we didn't know the real position; that estimate is
        # kept below ONLY as a fallback for untracked books with no recorded
        # position yet (e.g. a physical book you haven't logged progress for).
        if self.current_percent is not None:
            return min(100.0, max(0.0, self.current_percent))

        # ----- Legacy fallback: time/WPD estimate (no recorded position) -----
        if self.is_paused:
            return 0.0

        from ..services.settings_service import get_wpd_for_media
        from sqlalchemy.orm import object_session

        today = date.today()
        if self.date_started > today:
            return 0.0
        days_elapsed_today = (today - self.date_started).days + 1  # inclusive
        if days_elapsed_today <= 0:
            return 0.0

        db = object_session(self)
        if not db:
            if not self.effective_days_estimate:
                return None
            progress = (days_elapsed_today / self.effective_days_estimate) * 100
            return min(100.0, max(0.0, progress))

        wpd = get_wpd_for_media(db, self.media)
        words_read = days_elapsed_today * wpd
        progress = (words_read / self.book.word_count) * 100
        return min(100.0, max(0.0, progress))

    @property
    def current_progress_page(self) -> Optional[int]:
        """Calculate current page for IP books based on current progress percentage.

        Returns the page number the user should be on.
        Only works if the book has page_count.
        """
        if not self.is_started or self.is_finished:
            return None

        if not self.book or not self.book.page_count:
            return None

        progress_percent = self.current_progress_percent
        if progress_percent is None:
            return None

        return int((progress_percent / 100) * self.book.page_count)

    @property
    def rating_overall(self) -> Optional[float]:
        """Calculate overall rating as average of 5 core metrics (excluding horror and spice)."""
        core_ratings = [
            self.rating_enjoyment,
            self.rating_writing,
            self.rating_characters,
            self.rating_world_building,
            self.rating_readability
        ]

        # Filter out None values
        valid_ratings = [r for r in core_ratings if r is not None]

        if not valid_ratings:
            return None

        # Return average rounded to 1 decimal place
        return round(sum(valid_ratings) / len(valid_ratings), 1)

    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "id_previous": self.id_previous,
            "book_id": self.book_id,
            "media": self.media,
            "date_started": self.date_started.isoformat() if self.date_started else None,
            "date_finished_actual": self.date_finished_actual.isoformat() if self.date_finished_actual else None,
            "date_paused": self.date_paused.isoformat() if self.date_paused else None,
            "rating_horror": self.rating_horror,
            "rating_spice": self.rating_spice,
            "rating_world_building": self.rating_world_building,
            "rating_writing": self.rating_writing,
            "rating_characters": self.rating_characters,
            "rating_readability": self.rating_readability,
            "rating_enjoyment": self.rating_enjoyment,
            "rating_overall": self.rating_overall,
            "rank": self.rank,
            "days_estimate": self.days_estimate,
            "days_estimate_override": self.days_estimate_override,
            "calculated_days_estimate": self.calculated_days_estimate,
            "effective_days_estimate": self.effective_days_estimate,
            "date_est_start": self.date_est_start.isoformat() if self.date_est_start else None,
            "date_est_end": self.date_est_end.isoformat() if self.date_est_end else None,
            "date_finished_estimate": self.date_finished_estimate.isoformat() if self.date_finished_estimate else None,
            "actual_days_elapsed": self.actual_days_elapsed,
            "days_delta_from_estimate": self.days_delta_from_estimate,
            "reread": self.reread,
            "status": self.status,
            "is_finished": self.is_finished,
            "is_started": self.is_started,
            "is_paused": self.is_paused,
            "current_percent": self.current_percent,
            "current_percent_manual_override": self.current_percent_manual_override,
            "date_progress_set": self.date_progress_set.isoformat() if self.date_progress_set else None,
            "current_progress_percent": self.current_progress_percent,
            "current_progress_page": self.current_progress_page,
            # Include book data for convenience
            "book": self.book.to_dict() if self.book else None,
        }


# Pydantic models for API
class ReadingBase(BaseModel):
    """Base reading schema."""
    book_id: int
    media: Optional[str] = None
    date_started: Optional[date] = None
    date_finished_actual: Optional[date] = None
    rating_horror: Optional[float] = None
    rating_spice: Optional[float] = None
    rating_world_building: Optional[float] = None
    rating_writing: Optional[float] = None
    rating_characters: Optional[float] = None
    rating_readability: Optional[float] = None
    rating_enjoyment: Optional[float] = None
    rank: Optional[int] = None
    days_estimate: Optional[int] = None
    days_estimate_override: bool = False
    reread: bool = False
    current_percent: Optional[float] = None
    current_percent_manual_override: bool = False


class ReadingCreate(ReadingBase):
    """Schema for creating readings."""
    pass


class ReadingUpdate(ReadingBase):
    """Schema for updating readings."""
    book_id: Optional[int] = None


class ReadingResponse(ReadingBase):
    """Schema for reading responses."""
    id: int
    id_previous: Optional[int] = None
    date_est_start: Optional[date] = None
    date_est_end: Optional[date] = None
    calculated_days_estimate: Optional[int] = None
    effective_days_estimate: Optional[int] = None
    date_finished_estimate: Optional[date] = None
    actual_days_elapsed: Optional[int] = None
    days_delta_from_estimate: Optional[int] = None
    status: str
    is_finished: bool
    is_started: bool
    current_page: Optional[int] = None
    current_page_manual_override: bool = False
    current_progress_percent: Optional[float] = None
    current_progress_page: Optional[int] = None
    book: Optional['BookResponse'] = None  # Include book data

    class Config:
        from_attributes = True
