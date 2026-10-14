"""Wishlist auto-acquire state (#297).

One row per (book, format) the wishlist acquirer has looked at, so a run never
re-hammers the same book: it carries the outcome, the attempt counter and the next
time it is worth trying again. Created by create_tables() at startup (a brand-new
table, so plain create_all is enough — no column migration)."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint

from ..database import Base


class WishlistAcquireState(Base):
    __tablename__ = "wishlist_acquire_state"
    __table_args__ = (UniqueConstraint("book_id", "format", name="uq_wishlist_acquire_book_format"),)

    id = Column(Integer, primary_key=True)
    book_id = Column(Integer, ForeignKey("books.id"), nullable=False, index=True)
    format = Column(String, nullable=False)                 # ebook | audiobook
    status = Column(String, nullable=False, default="never_tried")
    attempts = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)       # None on a terminal status
    last_reason = Column(String, nullable=True)
    card_id = Column(String, nullable=True)                 # Libby card the borrow/hold used
    library = Column(String, nullable=True)                 # that card's library name/key
    libby_title_id = Column(String, nullable=True)          # that library's own OverDrive title id
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "format": self.format,
            "status": self.status,
            "attempts": self.attempts,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "next_attempt_at": self.next_attempt_at.isoformat() if self.next_attempt_at else None,
            "last_reason": self.last_reason,
            "card_id": self.card_id,
            "library": self.library,
            "libby_title_id": self.libby_title_id,
        }
