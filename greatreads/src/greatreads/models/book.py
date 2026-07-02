"""Book model."""

from datetime import date
from typing import Optional, List
from sqlalchemy import Column, Integer, String, Date, Float, Boolean, VARCHAR
from sqlalchemy.orm import relationship
from pydantic import BaseModel

from ..database import Base
from .tag import book_tags


class Book(Base):
    """Book database model."""

    __tablename__ = 'books'

    id = Column(Integer, primary_key=True)
    title = Column(VARCHAR, nullable=False)
    author_name_first = Column(VARCHAR)
    author_name_second = Column(VARCHAR)
    author_gender = Column(VARCHAR)
    word_count = Column(Integer)
    page_count = Column(Integer)
    date_published = Column(Date)
    universe = Column(VARCHAR)
    series = Column(VARCHAR)
    series_number = Column(Float)
    genre = Column(VARCHAR)
    cover = Column(Boolean, nullable=False, default=False)
    isbn_id = Column(Integer)
    description = Column(String)                 # synopsis (from Calibre comments / enrichment)
    public_rating = Column(Float)                # community/public rating 0–5, SEPARATE from the
                                                 # user's own ratings (which live on Reading)

    # Relationships
    readings = relationship("Reading", back_populates="book", cascade="all, delete-orphan")
    inventory = relationship("Inventory", back_populates="book", cascade="all, delete-orphan")
    tags = relationship("Tag", secondary=book_tags, back_populates="books")

    @property
    def words_per_page(self) -> Optional[float]:
        """Calculate words per page."""
        return self.word_count / self.page_count if self.page_count and self.word_count else None

    @property
    def year_published(self) -> Optional[int]:
        """Get publication year."""
        return self.date_published.year if self.date_published else None

    @property
    def author(self) -> Optional[str]:
        """Get full author name in 'First Last' format."""
        if not self.author_name_second and not self.author_name_first:
            return None
        name_parts = [self.author_name_first, self.author_name_second]
        return " ".join(filter(None, name_parts))

    @property
    def author_sorted(self) -> Optional[str]:
        """Get author name in 'Last, First' format."""
        if not self.author_name_second:
            return self.author_name_first
        if not self.author_name_first:
            return self.author_name_second
        return f"{self.author_name_second}, {self.author_name_first}"

    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "author_sorted": self.author_sorted,
            "author_name_first": self.author_name_first,
            "author_name_second": self.author_name_second,
            "author_gender": self.author_gender,
            "word_count": self.word_count,
            "page_count": self.page_count,
            "date_published": self.date_published.isoformat() if self.date_published else None,
            "year_published": self.year_published,
            "universe": self.universe,
            "series": self.series,
            "series_number": self.series_number,
            "genre": self.genre,
            "cover": self.cover,
            "isbn_id": self.isbn_id,
            "words_per_page": self.words_per_page,
            "tags": [tag.name for tag in self.tags] if self.tags else [],
            "description": self.description,
            "public_rating": self.public_rating,
        }


# Pydantic models for API
class BookBase(BaseModel):
    """Base book schema."""
    title: str
    author_name_first: Optional[str] = None
    author_name_second: Optional[str] = None
    author_gender: Optional[str] = None
    word_count: Optional[int] = None
    page_count: Optional[int] = None
    date_published: Optional[date] = None
    universe: Optional[str] = None
    series: Optional[str] = None
    series_number: Optional[float] = None
    genre: Optional[str] = None
    cover: bool = False
    isbn_id: Optional[int] = None
    tags: Optional[List[str]] = None


class BookCreate(BookBase):
    """Schema for creating books."""
    pass


class BookUpdate(BookBase):
    """Schema for updating books."""
    title: Optional[str] = None


class BookResponse(BookBase):
    """Schema for book responses."""
    id: int
    author: Optional[str] = None
    author_sorted: Optional[str] = None
    year_published: Optional[int] = None
    words_per_page: Optional[float] = None

    class Config:
        from_attributes = True
