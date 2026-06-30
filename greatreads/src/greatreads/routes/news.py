"""News API routes (#68 Phase A) — read-only release feed + watch-set management.

The feed is populated by a daily cron (`services.news_service.poll_releases`); the
`/poll` endpoint here is a manual "Check now" that runs the same poll in the
background (it can take minutes for a large author set, so we don't block the request).
"""

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..services import news_service

logger = logging.getLogger(__name__)
router = APIRouter()


class AuthorBody(BaseModel):
    author: str


class SeenBody(BaseModel):
    id: Optional[int] = None


@router.get("/")
async def get_feed(kind: Optional[str] = None, include_low: bool = True,
                   db: Session = Depends(get_db)):
    """Non-dismissed releases. `kind` optionally filters to 'upcoming' | 'new'."""
    items = news_service.list_news(db, kind=kind, include_low=include_low)
    return {
        "items": items,
        "unread": news_service.unread_count(db),
        "counts": {
            "upcoming": sum(1 for i in items if i["kind"] == "upcoming"),
            "new": sum(1 for i in items if i["kind"] == "new"),
        },
    }


@router.get("/unread-count")
async def get_unread_count(db: Session = Depends(get_db)):
    return {"count": news_service.unread_count(db)}


@router.get("/author-reads")
async def get_author_reads(author: str, db: Session = Depends(get_db)):
    """Books the user has finished by this author (for the card detail popup)."""
    return {"author": author, "books": news_service.author_finished_books(db, author)}


@router.get("/shelf")
async def get_shelf(status: str = "owned", search: Optional[str] = None,
                    skip: int = 0, limit: int = 60, sort_by: str = "author",
                    sort_order: str = "asc", cover: str = "all", db: Session = Depends(get_db)):
    """Unified Books-page feed (#88): cover cards for one status
    (owned | unowned | upcoming | new), paginated for infinite scroll."""
    return news_service.list_shelf(db, status=status, search=search, skip=skip, limit=limit,
                                   sort_by=sort_by, sort_order=sort_order, cover=cover)


@router.get("/series")
async def get_series_books(name: str, universe: Optional[str] = None,
                          db: Session = Depends(get_db)):
    """All books in a series (ordered by number) for the Series view (#96)."""
    return news_service.series_books(db, series=name, universe=universe)


@router.post("/seen")
async def post_seen(body: SeenBody, db: Session = Depends(get_db)):
    """Mark one item seen (with id) or all unseen items seen (no id) — clears the badge."""
    news_service.mark_seen(db, item_id=body.id)
    return {"unread": news_service.unread_count(db)}


@router.post("/{item_id}/dismiss")
async def post_dismiss(item_id: int, db: Session = Depends(get_db)):
    ok = news_service.dismiss(db, item_id)
    return {"ok": ok, "unread": news_service.unread_count(db)}


def _run_poll() -> None:
    db = SessionLocal()
    try:
        news_service.poll_releases(db)
        news_service.reprocess(db)                 # re-clean + drop stale junk leftovers
        news_service.enrich_with_openlibrary(db)   # OpenLibrary cross-ref (no Google quota)
    except Exception as exc:
        logger.error("manual news poll failed: %s", exc)
    finally:
        db.close()


@router.post("/poll")
async def post_poll(background_tasks: BackgroundTasks):
    """Trigger a release poll in the background ("Check now"). Returns immediately."""
    background_tasks.add_task(_run_poll)
    return {"started": True}


# ── watch-set management ─────────────────────────────────────────────────────
@router.get("/watch")
async def get_watch(db: Session = Depends(get_db)):
    return news_service.get_watch(db)


@router.post("/watch/exclude")
async def post_exclude(body: AuthorBody, db: Session = Depends(get_db)):
    news_service.add_excluded(db, body.author)
    return news_service.get_watch(db)


@router.post("/watch/unexclude")
async def post_unexclude(body: AuthorBody, db: Session = Depends(get_db)):
    news_service.remove_excluded(db, body.author)
    return news_service.get_watch(db)


@router.post("/watch/include")
async def post_include(body: AuthorBody, db: Session = Depends(get_db)):
    news_service.add_extra(db, body.author)
    return news_service.get_watch(db)


@router.post("/watch/uninclude")
async def post_uninclude(body: AuthorBody, db: Session = Depends(get_db)):
    news_service.remove_extra(db, body.author)
    return news_service.get_watch(db)
