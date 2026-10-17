"""Library-card borrow history (#357).

One row per AUTOMATIC borrow (wishlist auto-acquire #297, hold auto-fulfill #179), so
``services.card_budget`` can enforce rolling per-card / global limits and spread borrows
across cards. OverDrive rate-limits a card that churns borrow+return (124 borrows in 3 days
on one card). ``source`` is 'wishlist' | 'autofulfill' | 'seed' (backfilled once from
event_log). Created by create_tables() at startup (brand-new table: plain create_all)."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime

from ..database import Base


class CardBorrow(Base):
    __tablename__ = "card_borrows"

    id = Column(Integer, primary_key=True)
    card_id = Column(String, nullable=False, index=True)
    at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    source = Column(String, nullable=True)
    title = Column(String, nullable=True)
