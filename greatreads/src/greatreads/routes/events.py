"""Read API for the structured event log (#184) — powers the Settings → Logs page.

Phase 1 is read-only: events are written in-process via
services.event_log_service.log_event. A POST ingest for external emitters (the
acsm-watcher surfacing Calibre DeDRM/word-count steps) is deferred to phase 2 so it
can ship with proper machine auth.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models.user import User
from ..services.event_log_service import query_events, CATEGORIES, LEVELS

router = APIRouter()


@router.get("/logs")
async def get_logs(
    category: Optional[str] = None,
    level: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    before_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Newest-first activity-log rows, with optional category/level/search filters
    and id-based paging (``before_id`` = load older)."""
    events = query_events(db, category=category, level=level, q=q,
                          limit=limit, before_id=before_id)
    return {"events": events, "count": len(events)}


@router.get("/logs/meta")
async def get_logs_meta(current_user: User = Depends(get_current_user)):
    """Filter options for the Logs page."""
    return {"categories": CATEGORIES, "levels": LEVELS}
