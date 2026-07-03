"""Structured activity/event log (#184).

An append-only record of headless/background interactions — borrows, imports,
metadata scans, deploys, etc. — surfaced on the Settings → Logs page for
confidence + debugging. Written best-effort via services.event_log_service.log_event
(never breaks the calling operation) and created by create_tables() at startup.
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Index

from ..database import Base


class EventLog(Base):
    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    category = Column(String, nullable=False, index=True)   # libby | import | metadata | cover | system
    event = Column(String, nullable=False)                  # borrow | acsm_download | created | dismiss | enriched | deploy …
    level = Column(String, nullable=False, default="info")  # info | warn | error | success
    book_id = Column(Integer, nullable=True)                # optional link back to a book
    title = Column(String, nullable=True)                   # denormalized for display without a join
    detail = Column(String, nullable=True)                  # JSON blob of extra fields

    def to_dict(self) -> dict:
        import json
        try:
            detail = json.loads(self.detail) if self.detail else None
        except (ValueError, TypeError):
            detail = self.detail
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "category": self.category,
            "event": self.event,
            "level": self.level,
            "book_id": self.book_id,
            "title": self.title,
            "detail": detail,
        }


# Composite index for the common "filter by category, newest first" query.
Index("idx_event_cat_ts", EventLog.category, EventLog.ts.desc())
