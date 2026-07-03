"""GreatReads → Libby engine proxy (#142).

Thin, normalized proxy over the headless "Libby engine" sidecar (the `libby-web`
Flask app on :5007). Keeps all Libby secrets (the chip identity, per-library
website credentials) server-side — the browser only ever talks to GreatReads,
never to the engine directly.

MVP-1 (token self-service):
  - GET  /api/libby/status  → chip/token health (linked?, card count, token exp +
                              seconds remaining, can_fulfill) plus engine
                              reachability and a normalized traffic-light `state`.
  - POST /api/libby/relink  → re-link the chip from a phone-generated code
                              (engine runs get_chip() + clone_by_code() + sync()).

Milestone 3 (search + borrow/download) and 4 (holds + cards) forward to the
engine's existing routes; mostly pass-through, secrets stay server-side:
  - GET  /api/libby/search   → engine /api/search (Thunder catalog, normalized rows)
  - POST /api/libby/download → engine /api/download (borrow→fulfill→.acsm→watcher)
  - GET  /api/libby/loans    → engine /api/loans
  - GET  /api/libby/holds    → engine /api/holds
  - POST /api/libby/holds/{place,cancel,suspend,unsuspend} → engine /api/holds/*
  - POST /api/libby/return   → engine /api/loans/return
  - GET  /api/libby/cards, /api/libby/cards/status → engine /api/cards[/status]
  - GET  /api/libby/downloads → engine /api/downloads (server-side history)
"""

import asyncio
import logging
import os
import re

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models.user import User
from ..models.book import Book
from ..models.inventory import Inventory
from ..models.external_import import ExternalImport

logger = logging.getLogger(__name__)

router = APIRouter()

# The engine stays bound to the host; the GreatReads container reaches it via the
# host gateway (same mechanism as Calibre/ABS — see extra_hosts in the ereader
# compose). Overridable so a future compose-network alias (http://libby-web:5007)
# can be swapped in without a code change.
LIBBY_ENGINE_URL = os.environ.get("LIBBY_ENGINE_URL", "http://host.docker.internal:5007").rstrip("/")

_DAY = 86400
# Match §5: warn (amber badge) when the token expires within ~2 days; critical
# (red) within ~1 day; dead once expired.
_WARN_SECONDS = 2 * _DAY
_CRITICAL_SECONDS = _DAY

# Borrow→fulfill→.acsm can drive the Playwright OverDrive-website path, which is
# slow (borrow, sign in, poll the loans page, trigger the download). Give it room.
_DOWNLOAD_TIMEOUT = 180.0
_SEARCH_TIMEOUT = 45.0
_DEFAULT_TIMEOUT = 25.0


async def _engine_get(path: str, params=None, timeout: float = _DEFAULT_TIMEOUT) -> JSONResponse:
    """Forward a GET to the engine and mirror its JSON + status code. On an
    unreachable engine, return 502 with a clear message (no stack to the browser)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{LIBBY_ENGINE_URL}{path}", params=params)
    except Exception as exc:
        logger.warning("libby proxy GET %s: engine unreachable: %s", path, exc)
        return JSONResponse({"error": "Libby engine is unreachable — is the libby-web service running?"}, status_code=502)
    return _mirror(resp)


async def _engine_post(path: str, json_body: dict, timeout: float = _DEFAULT_TIMEOUT) -> JSONResponse:
    """Forward a POST to the engine and mirror its JSON + status code."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{LIBBY_ENGINE_URL}{path}", json=json_body)
    except Exception as exc:
        logger.warning("libby proxy POST %s: engine unreachable: %s", path, exc)
        return JSONResponse({"error": "Libby engine is unreachable — is the libby-web service running?"}, status_code=502)
    return _mirror(resp)


def _mirror(resp: httpx.Response) -> JSONResponse:
    try:
        data = resp.json()
    except Exception:
        data = {"error": (resp.text or "Libby engine returned a non-JSON response.")[:500]}
    return JSONResponse(data, status_code=resp.status_code)


def _health_state(engine_reachable: bool, status: dict) -> str:
    """Normalize the engine status into a traffic-light state the UI can render
    directly: unreachable | dead | critical | warn | ok | unknown."""
    if not engine_reachable:
        return "unreachable"
    seconds_left = status.get("seconds_left")
    if seconds_left is None:
        return "unknown"
    if seconds_left <= 0:
        return "dead"
    if seconds_left < _CRITICAL_SECONDS:
        return "critical"
    if seconds_left < _WARN_SECONDS or not status.get("linked"):
        return "warn"
    return "ok"


@router.get("/status")
async def libby_status(current_user: User = Depends(get_current_user)):
    """Return normalized Libby chip/token health for the Books-page widget.

    Always returns 200: when the engine is unreachable we report
    `engine_reachable:false` / `state:"unreachable"` so the UI degrades to a clear
    "engine down" message instead of erroring.
    """
    engine_reachable = True
    raw: dict = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{LIBBY_ENGINE_URL}/api/status")
            resp.raise_for_status()
            raw = resp.json()
    except Exception as exc:
        logger.warning("libby_status: engine unreachable at %s: %s", LIBBY_ENGINE_URL, exc)
        engine_reachable = False

    state = _health_state(engine_reachable, raw)
    return {
        "engine_reachable": engine_reachable,
        "state": state,
        # `stale` == the Libby button should show a warning badge (§5).
        "stale": state in {"unreachable", "dead", "critical", "warn"},
        "linked": bool(raw.get("linked")),
        "cards": raw.get("cards"),
        "exp": raw.get("exp"),
        "seconds_left": raw.get("seconds_left"),
        "can_fulfill": bool(raw.get("can_fulfill")),
        "prbn": raw.get("prbn"),
        "accounts": raw.get("accounts"),
        "sync_error": raw.get("sync_error"),
    }


class RelinkRequest(BaseModel):
    code: str


@router.post("/relink")
async def libby_relink(
    payload: RelinkRequest = Body(...),
    current_user: User = Depends(get_current_user),
):
    """Re-link the Libby chip from an 8-char phone code (Libby → Settings → Copy
    to Another Device). Proxies to the engine's POST /api/relink."""
    code = (payload.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Missing code — paste the 8-character code from Libby → Settings → Copy to Another Device.")

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{LIBBY_ENGINE_URL}/api/relink", json={"code": code})
    except Exception as exc:
        logger.warning("libby_relink: engine unreachable at %s: %s", LIBBY_ENGINE_URL, exc)
        raise HTTPException(status_code=502, detail="Libby engine is unreachable — is the libby-web service running?")

    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code >= 400 or not data.get("ok"):
        detail = data.get("error") or "Re-link failed. Double-check the code (it expires within a few minutes) and try again."
        raise HTTPException(status_code=resp.status_code if resp.status_code >= 400 else 502, detail=detail)

    return {
        "ok": True,
        "cards": data.get("cards"),
        "loans": data.get("loans"),
        "holds": data.get("holds"),
        "exp": data.get("exp"),
        "seconds_left": data.get("seconds_left"),
        "logged_in": data.get("logged_in"),
    }


# ── Milestone 3 — search + borrow/download ───────────────────────────────────

@router.get("/search")
async def libby_search(request: Request, current_user: User = Depends(get_current_user)):
    """Search library ebooks (OverDrive Thunder catalog via the engine).

    Author-name relevance fix (#142 item 1): the engine's text search expands the
    query into seed terms and drops books BY an author (a bare "stephen king"
    returns books ABOUT him, not his novels), whereas the engine's /api/author-books
    passes the raw query and DOES surface them. So on page 1 of a short query we
    fetch both in parallel and LEAD with genuine by-author matches (query tokens ⊆
    the item's author), then the normal text results — no dropdowns needed."""
    params = dict(request.query_params)
    q = (params.get("q") or "").strip()
    try:
        page = max(1, int(params.get("page") or "1"))
    except ValueError:
        page = 1
    # Author blend only makes sense for a short, name-like query on the first page.
    want_author = page == 1 and 1 <= len(q.split()) <= 4

    try:
        async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
            calls = [client.get(f"{LIBBY_ENGINE_URL}/api/search", params=params)]
            if want_author:
                calls.append(client.get(f"{LIBBY_ENGINE_URL}/api/author-books", params={"q": q}))
            responses = await asyncio.gather(*calls, return_exceptions=True)
    except Exception as exc:
        logger.warning("libby_search: engine unreachable: %s", exc)
        return JSONResponse({"error": "Libby engine is unreachable — is the libby-web service running?"}, status_code=502)

    base_resp = responses[0]
    if isinstance(base_resp, Exception):
        logger.warning("libby_search: base search failed: %s", base_resp)
        return JSONResponse({"error": "Libby search failed — the token may need a re-link."}, status_code=502)
    if base_resp.status_code >= 400:
        return _mirror(base_resp)
    try:
        base = base_resp.json()
    except Exception:
        return JSONResponse({"error": "Libby engine returned a non-JSON response."}, status_code=502)

    results = base.get("results") or []
    meta = base.get("meta") or {}

    if want_author and len(responses) > 1 and not isinstance(responses[1], Exception) and responses[1].status_code < 400:
        try:
            author_rows = responses[1].json().get("results") or []
        except Exception:
            author_rows = []
        q_tokens = set(_tokens(q))
        # Genuine by-author books: every query token appears in the item's author.
        leaders = [r for r in author_rows if q_tokens and q_tokens <= set(_tokens(r.get("author", "")))]
        if leaders:
            leaders.sort(key=lambda r: (not r.get("isAvailable", False), int(r.get("holdsCount") or 0), (r.get("title") or "").lower()))
            lead_ids = {str(r.get("id")) for r in leaders}
            rest = [r for r in results if str(r.get("id")) not in lead_ids]
            results = leaders + rest
            meta = {**meta, "total": (meta.get("total") or len(rest)) + len(leaders)}

    return JSONResponse({"results": results, "meta": meta, "query": base.get("query")})


class DownloadRequest(BaseModel):
    title_id: str
    card_id: str
    title: str | None = ""
    request_id: str | None = ""


@router.post("/download")
async def libby_download(
    payload: DownloadRequest = Body(...),
    current_user: User = Depends(get_current_user),
):
    """Borrow → fulfill → save the .acsm into the Calibre watcher dir. Synchronous
    (may run the Playwright OverDrive-website path), so allow a long timeout. The
    GreatReads book record is created only after the watcher import (one source of
    truth) — the caller refreshes the Newly-Imported tray on success."""
    if not payload.title_id or not payload.card_id:
        raise HTTPException(status_code=400, detail="title_id and card_id are required.")
    body = {
        "title_id": payload.title_id,
        "card_id": payload.card_id,
        "title": payload.title or "",
        "request_id": payload.request_id or "",
    }
    return await _engine_post("/api/download", body, timeout=_DOWNLOAD_TIMEOUT)


@router.get("/downloads")
async def libby_downloads(current_user: User = Depends(get_current_user)):
    """Server-side download history (from the engine)."""
    return await _engine_get("/api/downloads")


# ── Item 8 — rich metadata + full (foreign) series ───────────────────────────

@router.post("/book-details")
async def libby_book_details(
    payload: dict = Body(...),
    current_user: User = Depends(get_current_user),
):
    """Full metadata for a title: synopsis, subjects/genres, community rating,
    publisher, language, page count, series (name+id+index), per-library
    availability. Forwards {title_id, book} to the engine's /api/book-details."""
    if not str(payload.get("title_id", "")):
        raise HTTPException(status_code=400, detail="title_id is required.")
    return await _engine_post("/api/book-details", payload, timeout=_SEARCH_TIMEOUT)


@router.get("/series-books")
async def libby_series_books(request: Request, current_user: User = Depends(get_current_user)):
    """All titles in a series — including ones NOT in the GreatReads library — each
    with cover + per-library availability. Forwards series_id / series_name."""
    return await _engine_get("/api/series-books", params=dict(request.query_params), timeout=_SEARCH_TIMEOUT)


@router.get("/author-books")
async def libby_author_books(request: Request, current_user: User = Depends(get_current_user)):
    """All books by a creator (creator_id) or author-name query (q)."""
    return await _engine_get("/api/author-books", params=dict(request.query_params), timeout=_SEARCH_TIMEOUT)


# ── Milestone 4 — loans, holds, cards ────────────────────────────────────────

@router.get("/loans")
async def libby_loans(request: Request, current_user: User = Depends(get_current_user)):
    return await _engine_get("/api/loans", params=dict(request.query_params))


@router.get("/holds")
async def libby_holds(request: Request, current_user: User = Depends(get_current_user)):
    return await _engine_get("/api/holds", params=dict(request.query_params))


class TitleCardRequest(BaseModel):
    title_id: str
    card_id: str
    days: int | None = None


@router.post("/holds/{action}")
async def libby_hold_action(
    action: str,
    payload: TitleCardRequest = Body(...),
    current_user: User = Depends(get_current_user),
):
    """Place / cancel / suspend / unsuspend a hold (engine /api/holds/<action>)."""
    if action not in {"place", "cancel", "suspend", "unsuspend"}:
        raise HTTPException(status_code=404, detail="Unknown hold action.")
    if not payload.title_id or not payload.card_id:
        raise HTTPException(status_code=400, detail="title_id and card_id are required.")
    body: dict = {"title_id": payload.title_id, "card_id": payload.card_id}
    if action == "suspend":
        body["days"] = payload.days if payload.days is not None else 30
    return await _engine_post(f"/api/holds/{action}", body)


@router.post("/return")
async def libby_return(
    payload: TitleCardRequest = Body(...),
    current_user: User = Depends(get_current_user),
):
    """Return a borrowed title (engine /api/loans/return)."""
    if not payload.title_id or not payload.card_id:
        raise HTTPException(status_code=400, detail="title_id and card_id are required.")
    return await _engine_post("/api/loans/return", {"title_id": payload.title_id, "card_id": payload.card_id})


@router.get("/cards")
async def libby_cards(current_user: User = Depends(get_current_user)):
    return await _engine_get("/api/cards")


@router.get("/cards/status")
async def libby_cards_status(current_user: User = Depends(get_current_user)):
    """Cards with loan/hold counts + credential status. SANITIZED — the engine's
    payload carries the saved website password (and username); those never reach
    the browser (§9 secrets stay server-side). We expose only a boolean + a masked
    username so the UI can show which cards still need credentials."""
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(f"{LIBBY_ENGINE_URL}/api/cards/status")
    except Exception as exc:
        logger.warning("libby cards/status: engine unreachable: %s", exc)
        return JSONResponse({"error": "Libby engine is unreachable — is the libby-web service running?"}, status_code=502)
    if resp.status_code >= 400:
        return _mirror(resp)
    try:
        data = resp.json()
    except Exception:
        return JSONResponse({"error": "Libby engine returned a non-JSON response."}, status_code=502)
    for card in (data.get("cards") or []):
        user = str(card.pop("credUsername", "") or "")
        card.pop("credPassword", None)
        card["credUsernameMasked"] = (f"{user[:2]}***{user[-2:]}" if len(user) > 4 else ("*" * len(user))) if user else ""
    return JSONResponse(data, status_code=resp.status_code)


# ── Item 6 — GreatReads ownership annotation for Libby search rows ────────────
# Ownership must come from the GreatReads library (inventory/external_imports),
# not the engine's Calibre fuzzy match (which missed e.g. "The Way of Kings").

def _tokens(s: str) -> frozenset:
    return frozenset(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def _build_owned_index(db: Session) -> list[dict]:
    """One row per GreatReads book that is owned in some format, with title/author
    tokens + the Calibre external id (for a future 'Read in app' link)."""
    owned_ids = {
        r[0] for r in db.query(Inventory.book_id).filter(
            (Inventory.owned_ebook == True) | (Inventory.owned_physical == True) | (Inventory.owned_audio == True)  # noqa: E712
        ).all()
    }
    if not owned_ids:
        return []
    calibre = {
        r[0]: r[1] for r in db.query(ExternalImport.book_id, ExternalImport.external_id)
        .filter(ExternalImport.source == "calibre").all()
    }
    index = []
    for b in db.query(Book).filter(Book.id.in_(owned_ids)).all():
        index.append({
            "book_id": b.id,
            "tt": _tokens(b.title),
            "at": _tokens(b.author or ""),
            "calibre_id": calibre.get(b.id),
        })
    return index


def _match_owned(title: str, author: str, index: list[dict]) -> dict | None:
    """Return the best owned-book match for a (title, author), or None. Title Jaccard
    ≥ 0.6 with some author overlap, or a very strong title match (≥ 0.9) on its own."""
    in_t, in_a = _tokens(title), _tokens(author)
    if not in_t:
        return None
    best, best_sim = None, 0.0
    for b in index:
        union = len(in_t | b["tt"])
        sim = len(in_t & b["tt"]) / union if union else 0.0
        if sim < 0.6:
            continue
        if in_a and b["at"]:
            au = len(in_a | b["at"])
            asim = len(in_a & b["at"]) / au if au else 0.0
            if asim < 0.34 and sim < 0.9:
                continue  # title-only coincidence without author agreement
        if sim > best_sim:
            best, best_sim = b, sim
    return best


class OwnershipRequest(BaseModel):
    items: list[dict]   # [{title, author}]


@router.post("/ownership")
async def libby_ownership(
    payload: OwnershipRequest = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Annotate Libby search rows with real GreatReads ownership. Returns a parallel
    array: [{owned, book_id, calibre_id}] so the UI can flag owned titles and steer
    the action away from a redundant borrow (#142 item 6, §6 ownership)."""
    index = _build_owned_index(db)
    results = []
    for it in (payload.items or []):
        m = _match_owned(str(it.get("title", "")), str(it.get("author", "")), index) if index else None
        results.append({
            "owned": bool(m),
            "book_id": m["book_id"] if m else None,
            "calibre_id": m["calibre_id"] if m else None,
        })
    return {"results": results}


# ── Wishlist integration (#170) — add Libby titles/holds to the Wishlist ──────────
def _build_all_index(db: Session) -> list[dict]:
    """title/author tokens for EVERY book (owned or Wishlist), so we find-or-create
    without spawning duplicates."""
    return [{"book_id": b.id, "tt": _tokens(b.title), "at": _tokens(b.author or "")}
            for b in db.query(Book).filter(Book.title.isnot(None)).all()]


def _ensure_wishlist_book(db: Session, title: str, author: str, series, series_number, index):
    """Find a matching DB book or create an unowned Wishlist record. (book, created)."""
    m = _match_owned(title or "", author or "", index) if index else None
    if m:
        return db.query(Book).filter(Book.id == m["book_id"]).first(), False
    from ..services.import_service import _split_author
    first, second = _split_author(author or "")
    try:
        sn = float(series_number) if series_number not in (None, "") else None
    except (ValueError, TypeError):
        sn = None
    b = Book(title=(title or "").strip(), author_name_first=first or None,
             author_name_second=second or None, series=(series or None),
             series_number=sn, cover=False)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b, True


async def _save_cover(book_id: int, url: str) -> bool:
    if not url:
        return False
    from ..config import settings
    covers_dir = settings.covers_dir
    covers_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 GreatReads/cover-fetch"}) as c:
            r = await c.get(url)
        if r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("image/"):
            (covers_dir / f"{book_id}.jpg").write_bytes(r.content)
            return True
    except Exception:
        pass
    return False


class WishlistAddRequest(BaseModel):
    title: str
    author: str = ""
    series: str | None = None
    series_number: float | None = None
    cover_url: str | None = None


@router.post("/wishlist-add")
async def libby_wishlist_add(
    payload: WishlistAddRequest = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find-or-create an unowned Wishlist record for a Libby title (#170)."""
    if not (payload.title or "").strip():
        raise HTTPException(status_code=400, detail="Title required")
    index = _build_all_index(db)
    book, created = _ensure_wishlist_book(db, payload.title, payload.author,
                                          payload.series, payload.series_number, index)
    if created and payload.cover_url and await _save_cover(book.id, payload.cover_url):
        book.cover = True
        db.commit()
    return {"book_id": book.id, "created": created, "title": book.title}


@router.post("/holds-to-wishlist")
async def libby_holds_to_wishlist(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ensure every current Libby hold has a Wishlist record (#170 backfill)."""
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(f"{LIBBY_ENGINE_URL}/api/holds")
        holds = (resp.json() or {}).get("holds", []) if resp.status_code == 200 else []
    except Exception:
        raise HTTPException(status_code=502, detail="Libby engine unreachable")
    index = _build_all_index(db)
    added = existing = 0
    for h in holds:
        book, created = _ensure_wishlist_book(db, h.get("title", ""), h.get("author", ""),
                                              h.get("series"), h.get("seriesIndex"), index)
        if created:
            added += 1
            if h.get("cover") and await _save_cover(book.id, h["cover"]):
                book.cover = True
                db.commit()
            index.append({"book_id": book.id, "tt": _tokens(book.title), "at": _tokens(book.author or "")})
        else:
            existing += 1
    return {"added": added, "existing": existing, "total": len(holds)}


# ── Auto-fulfill ready holds (#179) ──────────────────────────────────────────

class AutofulfillConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_min: int | None = None
    max_per_run: int | None = None


def _reschedule_autofulfill(request: Request, interval_min: int) -> None:
    """Push a new interval to the live APScheduler job (#166 pattern)."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if not scheduler:
        return
    try:
        from apscheduler.triggers.interval import IntervalTrigger
        scheduler.reschedule_job("libby_autofulfill", trigger=IntervalTrigger(minutes=max(1, interval_min)))
    except Exception as exc:
        logger.warning("Could not reschedule libby_autofulfill: %s", exc)


@router.get("/autofulfill-config")
async def libby_autofulfill_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from ..services.libby_autofulfill_service import get_config
    return get_config(db)


@router.post("/autofulfill-config")
async def libby_autofulfill_set_config(
    request: Request,
    payload: AutofulfillConfigRequest = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from ..services.libby_autofulfill_service import (
        get_config, _set, SETTING_ENABLED, SETTING_INTERVAL, SETTING_MAX,
    )
    if payload.enabled is not None:
        _set(db, SETTING_ENABLED, "1" if payload.enabled else "0")
    if payload.interval_min is not None:
        iv = max(1, int(payload.interval_min))
        _set(db, SETTING_INTERVAL, iv)
        _reschedule_autofulfill(request, iv)
    if payload.max_per_run is not None:
        _set(db, SETTING_MAX, max(1, int(payload.max_per_run)))
    return get_config(db)


@router.post("/autofulfill-run")
async def libby_autofulfill_run(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Manual 'Run now' — runs one pass even when the toggle is off."""
    from ..services.libby_autofulfill_service import run_autofulfill
    return run_autofulfill(db, force=True)
