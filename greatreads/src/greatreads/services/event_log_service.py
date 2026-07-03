"""Write/read helpers for the structured event log (#184).

``log_event`` is best-effort and NEVER raises into the caller — logging must not be
able to break a borrow / import / scan. It opens its own short-lived session so it's
decoupled from the caller's transaction (an event is committed even if the caller
later rolls back, and vice-versa).
"""

import json
import logging
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models.event_log import EventLog

logger = logging.getLogger(__name__)

# Known categories (for the UI filter + light validation). Unknown categories are
# still accepted — this is a hint list, not a hard allowlist.
CATEGORIES = ["libby", "import", "metadata", "cover", "system"]
LEVELS = ["info", "success", "warn", "error"]

# Keep the log bounded — events are low-volume, but prune so it can't grow forever.
MAX_ROWS = 20000


def log_event(category: str, event: str, *, level: str = "info",
              book_id: Optional[int] = None, title: Optional[str] = None,
              detail: Optional[dict] = None, **extra) -> None:
    """Record one event. Best-effort; swallows all errors."""
    from ..database import SessionLocal
    payload = None
    merged = {**(detail or {}), **extra}
    if merged:
        try:
            payload = json.dumps(merged, default=str)
        except (TypeError, ValueError):
            payload = None
    try:
        db = SessionLocal()
        try:
            db.add(EventLog(
                category=str(category), event=str(event),
                level=(level if level in LEVELS else "info"),
                book_id=book_id, title=(title[:300] if title else None),
                detail=payload,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:   # noqa: BLE001 — logging must never break the caller
        logger.warning("log_event failed (%s/%s): %s", category, event, exc)


def query_events(db: Session, *, category: Optional[str] = None,
                 level: Optional[str] = None, q: Optional[str] = None,
                 limit: int = 200, before_id: Optional[int] = None) -> list:
    """Newest-first event rows for the Logs page, with optional filters."""
    query = db.query(EventLog)
    if category:
        query = query.filter(EventLog.category == category)
    if level:
        query = query.filter(EventLog.level == level)
    if before_id:
        query = query.filter(EventLog.id < before_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (EventLog.event.ilike(like)) | (EventLog.title.ilike(like)) | (EventLog.detail.ilike(like))
        )
    limit = max(1, min(limit, 1000))
    return [e.to_dict() for e in query.order_by(desc(EventLog.id)).limit(limit).all()]


def prune_events(db: Optional[Session] = None, keep: int = MAX_ROWS) -> int:
    """Trim the log to the newest ``keep`` rows. Returns rows deleted."""
    own = db is None
    if own:
        from ..database import SessionLocal
        db = SessionLocal()
    try:
        total = db.query(EventLog.id).count()
        if total <= keep:
            return 0
        # id of the newest row to keep; delete everything older.
        cutoff = (
            db.query(EventLog.id).order_by(desc(EventLog.id)).offset(keep).limit(1).scalar()
        )
        if cutoff is None:
            return 0
        deleted = db.query(EventLog).filter(EventLog.id <= cutoff).delete(synchronize_session=False)
        db.commit()
        return deleted
    except Exception as exc:   # noqa: BLE001
        logger.warning("prune_events failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        if own:
            db.close()
