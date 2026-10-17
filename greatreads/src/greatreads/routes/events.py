"""Read API for the structured event log (#184) — powers the Settings → Logs page.

Events are written in-process via services.event_log_service.log_event. The one
external emitter is the Calibre acsm-watcher, via ``POST /events/ingest`` (#351),
limited to loopback / private source addresses (no secrets).
"""

import ipaddress
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models.book import Book
from ..models.external_import import ExternalImport
from ..models.user import User
from ..services import event_log_service
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


# --- Ingest for external emitters (#351) -------------------------------------------

INGEST_EVENTS = {"picked_up", "added", "finalized", "failed", "skipped_duplicate"}
_DETAIL_MAX_KEYS = 20
_DETAIL_MAX_STR = 300
_DETAIL_MAX_JSON = 2000


class IngestEvent(BaseModel):
    category: str
    event: str
    level: Optional[str] = None
    title: Optional[str] = None
    book_id: Optional[int] = None
    calibre_id: Optional[int] = None
    detail: Optional[dict] = None


def _is_local_source(host: Optional[str]) -> bool:
    """Loopback or private source address (docker gateway, LAN, Tailscale 100.64/10)."""
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_private
            or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")))


def _clamp_detail(detail: Optional[dict]) -> dict:
    """A small, flat, JSON-safe copy of ``detail``: capped keys, string lengths and total size."""
    out: dict = {}
    for k, v in list((detail or {}).items())[:_DETAIL_MAX_KEYS]:
        if isinstance(v, (int, float, bool)) or v is None:
            out[str(k)[:40]] = v
        else:
            out[str(k)[:40]] = str(v)[:_DETAIL_MAX_STR]
    while out and len(str(out)) > _DETAIL_MAX_JSON:
        out.popitem()
    return out


def ingest_event(db: Session, body: IngestEvent, client_host: Optional[str]) -> dict:
    """Validate and record one external event (the testable core of the route)."""
    if not _is_local_source(client_host):
        raise HTTPException(status_code=403, detail="Event ingest is limited to local addresses")
    if body.category != "calibre":
        raise HTTPException(status_code=422, detail="Only category 'calibre' is accepted")
    if body.event not in INGEST_EVENTS:
        raise HTTPException(status_code=422, detail=f"Unknown event (allowed: {sorted(INGEST_EVENTS)})")

    book_id = body.book_id
    if body.calibre_id is not None:
        link = db.query(ExternalImport).filter(
            ExternalImport.source == "calibre",
            ExternalImport.external_id == str(body.calibre_id)).first()
        if link:
            book_id = link.book_id
    book = db.query(Book).filter(Book.id == book_id).first() if book_id is not None else None
    if book is None:
        book_id = None   # never log a book id we can't back
    # The watcher only knows the filename; once the Calibre id resolves to a book, its
    # real title wins so /logs and the Book Report match the import event (#353).
    if book is not None and book.title:
        title = book.title[:300]
    else:
        from ..services.bookworm_service import display_title
        title = display_title("calibre", (body.title or "").strip()[:300]) or None

    detail = _clamp_detail(body.detail)
    if body.calibre_id is not None:
        detail["calibre_id"] = body.calibre_id
    event_log_service.log_event(
        "calibre", body.event, level=(body.level or "info")[:20],
        book_id=book_id, title=title, detail=detail)
    return {"ok": True, "book_id": book_id, "title": title}


@router.post("/events/ingest")
async def post_ingest_event(body: IngestEvent, request: Request, db: Session = Depends(get_db)):
    """Record a Calibre watcher step (``calibre/*``). No auth: loopback/private IPs only."""
    return ingest_event(db, body, request.client.host if request.client else None)
