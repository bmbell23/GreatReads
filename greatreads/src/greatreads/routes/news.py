"""News API routes (#68 Phase A) — read-only release feed + watch-set management.

The feed is populated by a daily cron (`services.news_service.poll_releases`); the
`/poll` endpoint here is a manual "Check now" that runs the same poll in the
background (it can take minutes for a large author set, so we don't block the request).
"""

import logging
import re
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..services import news_service

logger = logging.getLogger(__name__)
router = APIRouter()

# Resolved fallback covers (#143). Keyed by normalized title|author; value is the
# artwork URL or None (negative cache) so we don't re-hit iTunes on every render.
_cover_cache: dict[str, Optional[str]] = {}


def _norm_cover(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


@router.get("/cover")
async def news_cover(title: str, author: str = ""):
    """Fallback cover resolver (#143). When Google Books has no cover for an
    Upcoming/New title, find one on Apple Books (iTunes Search — free/keyless) by
    title+author, VALIDATING that the match is really the same title (a naive query
    matched 'The Girl Who Looked Up' → 'Writers of the Future'). 302 → hi-res
    artwork on a confident match, else 404 so the client shows the parchment."""
    tnorm = _norm_cover(title)
    if not tnorm:
        raise HTTPException(status_code=404, detail="no title")
    key = f"{tnorm}|{_norm_cover(author)}"
    if key in _cover_cache:
        url = _cover_cache[key]
        if url:
            return RedirectResponse(url)
        raise HTTPException(status_code=404, detail="no cover")

    url: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get("https://itunes.apple.com/search", params={
                "term": f"{title} {author}".strip(), "entity": "ebook", "limit": 5,
            })
        results = resp.json().get("results", []) if resp.status_code == 200 else []
        tset = set(tnorm.split())
        for r in results:
            cand = _norm_cover(r.get("trackName", ""))
            cset = set(cand.split())
            # Accept exact, prefix, or full-title-token-subset — reject unrelated hits.
            if cand and (cand == tnorm or cand.startswith(tnorm) or (tset and tset <= cset)):
                art = r.get("artworkUrl100") or r.get("artworkUrl60") or ""
                if art:
                    url = re.sub(r"/\d+x\d+bb\.(jpg|png|jpeg)", r"/600x600bb.\1", art)
                    break
    except Exception as exc:
        logger.warning("news_cover(%r) fallback failed: %s", title, exc)

    _cover_cache[key] = url
    if url:
        return RedirectResponse(url)
    raise HTTPException(status_code=404, detail="no cover")


class AuthorBody(BaseModel):
    author: str


class SeenBody(BaseModel):
    id: Optional[int] = None
    kind: Optional[str] = None   # #188: mark seen only within one category (new|upcoming)


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


@router.get("/unread-by-kind")
async def get_unread_by_kind(db: Session = Depends(get_db)):
    """Unseen counts per category for the Store 'New (N)'/'Upcoming (N)' chips (#188)."""
    return news_service.unread_by_kind(db)


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


@router.get("/author")
async def get_author_books(name: str, db: Session = Depends(get_db)):
    """All DB books by an author (any ownership) for the author view (#155)."""
    return news_service.author_books(db, name)


@router.get("/genre")
async def get_genre_books(name: str, db: Session = Depends(get_db)):
    """All DB books tagged with a genre (any ownership) for the genre view (#155)."""
    return news_service.genre_books(db, name)


@router.get("/narrator")
async def get_narrator_books(name: str, db: Session = Depends(get_db)):
    """All DB audiobooks narrated by this person (any ownership) for the narrator view (#190)."""
    return news_service.narrator_books(db, name)


@router.post("/seen")
async def post_seen(body: SeenBody, db: Session = Depends(get_db)):
    """Mark one item seen (id), all in a category seen (kind), or everything (neither)."""
    news_service.mark_seen(db, item_id=body.id, kind=body.kind)
    return {"unread": news_service.unread_count(db), "by_kind": news_service.unread_by_kind(db)}


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


@router.post("/reprocess")
async def post_reprocess(db: Session = Depends(get_db)):
    """Re-clean existing items with the current filters — NO Google quota (#68). Applies
    tuned junk/ownership rules to already-cached rows (e.g. drop stale 'Sneak Peek'/'Untitled'
    placeholders), then re-runs the OpenLibrary cross-ref."""
    result = news_service.reprocess(db)
    news_service.enrich_with_openlibrary(db)
    return {"ok": True, **result, "unread": news_service.unread_count(db)}


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
