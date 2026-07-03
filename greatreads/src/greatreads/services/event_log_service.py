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

# Keep the log bounded. Retention is primarily TIME-based (keep the last ~90 days so
# there's real history to look back on), with a generous row cap as a safety net.
MAX_AGE_DAYS = 90
MAX_ROWS = 100000


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


def prune_events(db: Optional[Session] = None, keep: int = MAX_ROWS,
                 max_age_days: int = MAX_AGE_DAYS) -> int:
    """Prune the log: drop rows older than ``max_age_days``, then trim to the newest
    ``keep`` rows as a safety cap. Returns rows deleted."""
    from datetime import datetime, timedelta
    own = db is None
    if own:
        from ..database import SessionLocal
        db = SessionLocal()
    try:
        deleted = 0
        # 1) Time-based: keep the last N days of history.
        cutoff_dt = datetime.utcnow() - timedelta(days=max_age_days)
        deleted += db.query(EventLog).filter(EventLog.ts < cutoff_dt).delete(synchronize_session=False)
        # 2) Safety row cap: if still over `keep`, drop the oldest beyond it.
        total = db.query(EventLog.id).count()
        if total > keep:
            cutoff = db.query(EventLog.id).order_by(desc(EventLog.id)).offset(keep).limit(1).scalar()
            if cutoff is not None:
                deleted += db.query(EventLog).filter(EventLog.id <= cutoff).delete(synchronize_session=False)
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
