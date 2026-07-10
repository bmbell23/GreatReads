"""Main FastAPI application."""

import logging
import os
import threading
from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy.orm import Session

from .config import settings
from .database import create_tables, get_db, SessionLocal
from .routes import books, readings, chains, library, settings as settings_routes, stats, auth, inventory, reports, import_routes, bookshelves, news, enrichment, libby, events
from . import ereader_api  # absorbed Ereader backend (:8091), routes carry /api/... (#22)
from .auth import get_current_user_from_cookie

logger = logging.getLogger(__name__)


class CachedStaticFiles(StaticFiles):
    """StaticFiles that adds a Cache-Control header so browsers cache assets
    (e.g. book covers) and don't re-request them on every page refresh."""

    def __init__(self, *args, max_age: int = 86400, **kwargs):
        self.max_age = max_age
        super().__init__(*args, **kwargs)

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = f"public, max-age={self.max_age}"
        return response


class ForwardedPrefixMiddleware(BaseHTTPMiddleware):
    """Middleware to handle X-Forwarded-Prefix header for proper URL generation."""

    async def dispatch(self, request: Request, call_next):
        # Skip middleware for static files to avoid interfering with static file serving
        if request.url.path.startswith("/static/"):
            response = await call_next(request)
            return response

        # Get the forwarded prefix from nginx
        forwarded_prefix = request.headers.get("X-Forwarded-Prefix", "")
        if forwarded_prefix:
            # Set the root path for this request
            request.scope["root_path"] = forwarded_prefix

        response = await call_next(request)
        # Dynamic HTML pages + API JSON must never be served stale (recurring stale-page
        # issue). Static assets/covers are excluded above and keep their own long cache.
        response.headers["Cache-Control"] = "no-store"
        return response


def _midnight_recalculate():
    """Scheduled job: recalculate all reading chains at midnight MT."""
    db = SessionLocal()
    try:
        from .services.chain_calculator import ChainCalculator
        logger.info("Midnight chain recalculation starting…")
        ChainCalculator(db).recalculate_all_chains()
        logger.info("Midnight chain recalculation complete.")
    except Exception as exc:
        logger.error("Midnight chain recalculation failed: %s", exc)
    finally:
        db.close()


# Safety-net full sync cadence (minutes). The watcher below handles the fast path;
# this only backstops a missed filesystem event. Env-tunable.
AUTO_SYNC_INTERVAL_MINUTES = int(os.environ.get("AUTO_SYNC_INTERVAL_MINUTES", "15"))
# Coalesce a burst of Calibre writes (metadata.db + WAL + checkpoint) into one sync.
SYNC_DEBOUNCE_SECONDS = float(os.environ.get("SYNC_DEBOUNCE_SECONDS", "3"))

_watch_timer_lock = threading.Lock()
_watch_timer: "threading.Timer | None" = None


def _run_sync(reason: str):
    """Import new Calibre/ABS items into GreatReads (own session per run)."""
    db = SessionLocal()
    try:
        from .services.sync_service import sync_all
        summary = sync_all(db)
        logger.info("Auto-sync (%s): %s", reason, summary)
    except Exception as exc:
        logger.error("Auto-sync failed (%s): %s", reason, exc)
    finally:
        db.close()


def _auto_sync():
    """Scheduled safety-net full sync (the watcher covers the instant path)."""
    _run_sync("scheduled")


def _schedule_watch_sync():
    """Debounce filesystem events into a single sync a few seconds after writes settle."""
    global _watch_timer
    with _watch_timer_lock:
        if _watch_timer is not None:
            _watch_timer.cancel()
        _watch_timer = threading.Timer(SYNC_DEBOUNCE_SECONDS, _run_sync, args=("file change",))
        _watch_timer.daemon = True
        _watch_timer.start()


def _start_library_watcher():
    """Watch the Calibre (and ABS) DB directories and sync shortly after any change (#16).
    Returns the running Observer, or None if watchdog/the dirs are unavailable — the
    scheduled safety-net sync still covers that case. /calibre is a local bind mount,
    so inotify fires on host-side Calibre writes."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except Exception as exc:
        logger.warning("watchdog unavailable (%s); relying on scheduled sync only.", exc)
        return None

    class _DBChangeHandler(FileSystemEventHandler):
        def on_any_event(self, event):
            if getattr(event, "is_directory", False):
                return
            name = os.path.basename(getattr(event, "src_path", "") or "")
            # Calibre (metadata.db*) AND Audiobookshelf (absdatabase.sqlite*) SQLite DBs,
            # including their WAL/journal/shm sidecars.
            if ".db" in name or ".sqlite" in name:
                _schedule_watch_sync()

    observer = Observer()
    watched = []
    for path in {settings.calibre_library_path, os.path.dirname(settings.abs_db_path)}:
        if path and os.path.isdir(path):
            observer.schedule(_DBChangeHandler(), path, recursive=False)
            watched.append(path)
    if not watched:
        logger.warning("No watchable library dirs; relying on scheduled sync only.")
        return None
    observer.daemon = True
    observer.start()
    logger.info("Library watcher started on %s (debounce %.1fs).", ", ".join(watched), SYNC_DEBOUNCE_SECONDS)
    return observer


def _poll_news():
    """Scheduled job: poll Google Books for new/upcoming releases by watched authors (#68)."""
    db = SessionLocal()
    try:
        from .services.news_service import poll_releases, reprocess, enrich_with_openlibrary
        logger.info("Daily news poll starting…")
        result = poll_releases(db)
        reprocess(db)                          # re-clean titles/junk + drop stale junk leftovers
        enrich = enrich_with_openlibrary(db)   # cross-reference OpenLibrary (no Google quota)
        logger.info("Daily news poll complete: %s | enrich: %s", result, enrich)
    except Exception as exc:
        logger.error("Daily news poll failed: %s", exc)
    finally:
        db.close()


def _metadata_backfill():
    """Scheduled sweep (#159): fill empty synopsis/genre/public-rating/date/pages on
    library books from Apple/Google/OpenLibrary, spending a bounded slice of the daily
    Google quota. Backfill-only — never clobbers Calibre/user values."""
    db = SessionLocal()
    try:
        from .services.metadata_backfill_service import backfill_batch
        result = backfill_batch(db)
        if result.get("updated"):
            logger.info("Metadata backfill: %s", result)
    except Exception as exc:
        logger.error("Metadata backfill failed: %s", exc)
    finally:
        db.close()


def _libby_autofulfill():
    """Scheduled auto-fulfill of ready Libby holds (#179): borrow → download →
    confirm import → return. Opt-in (no-op unless the Settings toggle is on)."""
    db = SessionLocal()
    try:
        from .services.libby_autofulfill_service import run_autofulfill
        run_autofulfill(db)
    except Exception as exc:
        logger.error("Libby auto-fulfill failed: %s", exc)
    finally:
        db.close()


def _align_refresh():
    """Scheduled sweep (#266): rebuild the dense ebook↔audiobook chapter-alignment
    maps for every dual-format book whose ebook chapter list has been uploaded (i.e.
    opened at least once). Keeps cross-format progress sync tight as ABS chapters or
    the match logic change. Reader-assisted, so never-opened books are skipped."""
    try:
        from .ereader_api import refresh_all_alignments
        result = refresh_all_alignments()
        if result.get("books"):
            logger.info("Chapter alignment refresh: %s", result)
    except Exception as exc:
        logger.error("Chapter alignment refresh failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    create_tables()

    # Record the deploy/version as an event (#184) + prune the log so it stays bounded.
    try:
        from .services.event_log_service import log_event, prune_events
        from .ereader_api import _read_version, _read_build_stamp
        log_event("system", "deploy", level="success",
                  detail={"version": _read_version(), "build": _read_build_stamp(),
                          "modified": os.environ.get("GREATREADS_BUILD_MODIFIED", "")})
        prune_events()
    except Exception as _e:
        logger.warning("startup event-log write failed: %s", _e)

    # Background schedulers are opt-out so a vendored/second copy of GreatReads
    # (e.g. the isolated :8092 instance inside the Ereader repo) doesn't auto-sync
    # or write the DB behind our back. Default stays enabled for production.
    if os.environ.get("ENABLE_SCHEDULERS", "true").lower() != "true":
        logger.info("Schedulers disabled (ENABLE_SCHEDULERS=false); skipping background jobs.")
        yield
        return

    # ── Midnight chain-recalculation scheduler (Mountain Time) ──────────
    try:
        from datetime import datetime, timedelta
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BackgroundScheduler(timezone="America/Denver")
        scheduler.add_job(
            _midnight_recalculate,
            CronTrigger(hour=0, minute=0, timezone="America/Denver"),
            id="midnight_chain_recalc",
            replace_existing=True,
        )
        # Poll Calibre/ABS for new items and import them automatically.
        # Runs once shortly after startup, then on a fixed interval.
        scheduler.add_job(
            _auto_sync,
            IntervalTrigger(minutes=AUTO_SYNC_INTERVAL_MINUTES),
            id="auto_sync_libraries",
            replace_existing=True,
            next_run_time=datetime.now(),
            coalesce=True,
            max_instances=1,
        )
        # Daily News poll (#68) — Google Books is keyed; off-hours to spread quota.
        scheduler.add_job(
            _poll_news,
            CronTrigger(hour=5, minute=30, timezone="America/Denver"),
            id="daily_news_poll",
            replace_existing=True,
        )
        # Metadata backfill sweep (#159) — keeps the daily Google quota busy filling
        # empty synopsis/genre/rating/date/pages on library books, a small batch each run.
        # Interval is UI-configurable (#166), persisted in user_settings.
        _bf_db = SessionLocal()
        try:
            from .services.metadata_backfill_service import effective_interval
            _bf_interval = effective_interval(_bf_db)
        except Exception:
            _bf_interval = int(os.environ.get("METADATA_BACKFILL_INTERVAL_MIN", "60"))
        finally:
            _bf_db.close()
        scheduler.add_job(
            _metadata_backfill,
            IntervalTrigger(minutes=_bf_interval),
            id="metadata_backfill",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(minutes=3),
            coalesce=True,
            max_instances=1,
        )
        # Libby auto-fulfill sweep (#179) — borrows ready holds, confirms the import,
        # then returns. No-op unless the Settings toggle is on; interval UI-configurable.
        _af_db = SessionLocal()
        try:
            from .services.libby_autofulfill_service import effective_interval as _af_effective_interval
            _af_interval = _af_effective_interval(_af_db)
        except Exception:
            _af_interval = int(os.environ.get("LIBBY_AUTOFULFILL_INTERVAL_MIN", "30"))
        finally:
            _af_db.close()
        scheduler.add_job(
            _libby_autofulfill,
            IntervalTrigger(minutes=_af_interval),
            id="libby_autofulfill",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(minutes=5),
            coalesce=True,
            max_instances=1,
        )
        # Chapter-alignment refresh sweep (#266) — re-match the dense ebook↔audiobook
        # per-chapter maps for opened dual-format books, ~every 30 min.
        scheduler.add_job(
            _align_refresh,
            IntervalTrigger(minutes=int(os.environ.get("ALIGN_REFRESH_INTERVAL_MIN", "30"))),
            id="chapter_alignment_refresh",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(minutes=4),
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()
        app.state.scheduler = scheduler   # so /books/backfill-config can reschedule live (#166)
        logger.info(
            "Schedulers started: midnight chain-recalc + auto-sync safety net every %d min + daily news poll.",
            AUTO_SYNC_INTERVAL_MINUTES,
        )
    except Exception as exc:
        logger.error("Failed to start scheduler: %s", exc)
        scheduler = None

    # Instant Calibre/ABS change watcher (#16) — near-real-time imports.
    library_watcher = _start_library_watcher()

    yield

    # Shutdown
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
    if library_watcher is not None:
        library_watcher.stop()


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Interactive Reading Tracker",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add middleware for handling forwarded prefix
app.add_middleware(ForwardedPrefixMiddleware)

# Mount static files
# In Docker mode, mount covers directory separately since it's in /app/data
# IMPORTANT: Mount covers BEFORE general static to ensure it takes precedence
if settings.is_docker:
    from pathlib import Path
    covers_path = Path("/app/data/covers")
    covers_path.mkdir(parents=True, exist_ok=True)
    # Covers rarely change for a given book_id; cache them in the browser for a day
    # so refreshing the bookshelves/library views doesn't re-request every image.
    app.mount("/static/covers", CachedStaticFiles(directory=str(covers_path), max_age=86400), name="covers")

# Mount general static files (CSS, JS, etc.)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

# Setup templates
templates = Jinja2Templates(directory=str(settings.templates_dir))


def _asset_ver() -> str:
    """Cache-buster for JS/CSS assets — the newest mtime among the bundled JS files,
    so a code change (baked into a fresh image) invalidates the browser cache without
    a manual version bump. Falls back to the app version."""
    try:
        js_dir = settings.static_dir / "js"
        return str(int(max(p.stat().st_mtime for p in js_dir.glob("*.js"))))
    except Exception:
        return settings.app_version


templates.env.globals["asset_ver"] = _asset_ver()

# Include routers
# Auth router includes both API routes and page routes, so include it without prefix
app.include_router(auth.router, tags=["auth"])
app.include_router(books.router, prefix="/api/books", tags=["books"])
app.include_router(readings.router, prefix="/api/readings", tags=["readings"])
app.include_router(chains.router, prefix="/api/chains", tags=["chains"])
app.include_router(library.router, prefix="/api/library", tags=["library"])
app.include_router(inventory.router, prefix="/api/inventory", tags=["inventory"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["settings"])
app.include_router(stats.router, prefix="/api/stats", tags=["stats"])
app.include_router(reports.router, prefix="/api/reports", tags=["reports"])
app.include_router(import_routes.router, prefix="/api/import", tags=["import"])
app.include_router(bookshelves.router, prefix="/api/bookshelves", tags=["bookshelves"])
app.include_router(news.router, prefix="/api/news", tags=["news"])
app.include_router(enrichment.router, prefix="/api/enrichment", tags=["enrichment"])
app.include_router(libby.router, prefix="/api/libby", tags=["libby"])  # Libby/OverDrive proxy (#142)
app.include_router(events.router, prefix="/api", tags=["events"])  # activity/event log (#184)
app.include_router(ereader_api.router, tags=["ereader"])  # no prefix — routes already carry /api/... (#22)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, db: Session = Depends(get_db)):
    """Main page."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "index.html", {"current_user": current_user})


@app.get("/tbr", response_class=HTMLResponse)
async def tbr_page(request: Request, db: Session = Depends(get_db)):
    """TBR (To Be Read) page."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "tbr.html", {"current_user": current_user})


@app.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request, db: Session = Depends(get_db)):
    """Reading Journal page."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "journal.html", {"current_user": current_user})


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request, db: Session = Depends(get_db)):
    """Library browsing page."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "library.html", {"current_user": current_user})


@app.get("/bookshelves", response_class=HTMLResponse)
async def bookshelves_page(request: Request, db: Session = Depends(get_db)):
    """Physical bookshelves view."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "bookshelves.html", {"current_user": current_user})


@app.get("/books", response_class=HTMLResponse)
async def books_page(request: Request, db: Session = Depends(get_db)):
    """Books management page."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "books.html", {"current_user": current_user})


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, db: Session = Depends(get_db)):
    """Reading statistics page."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "stats.html", {"current_user": current_user})


@app.get("/highlights", response_class=HTMLResponse)
async def highlights_page(request: Request, db: Session = Depends(get_db)):
    """Highlights & bookmarks page (data served by the Ereader API)."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "highlights.html", {"current_user": current_user})


@app.get("/news")
async def news_page(request: Request):
    """Legacy /news URL — the page is now the unified Books page (#88). Redirect."""
    return RedirectResponse(url=request.url_for("books_page"))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    """User settings page."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "settings.html", {"current_user": current_user})


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, db: Session = Depends(get_db)):
    """Activity / event log page (#184)."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "logs.html", {"current_user": current_user})


@app.get("/api/covers/thumb/{book_id}")
async def cover_thumbnail(book_id: int):
    """Serve a small JPEG thumbnail (68 × auto px) for a book cover.

    Generates the thumbnail on first request using Pillow, then caches it in
    /app/data/covers_thumb/ so subsequent requests are instant file reads.
    Returns 404 when no source cover exists.
    """
    from PIL import Image  # Pillow is a dev dependency; import lazily

    THUMB_W = 68  # 2× retina for the 34 px spine slot
    src = Path("/app/data/covers") / f"{book_id}.jpg"
    if not src.exists():
        return Response(status_code=404)

    thumb_dir = Path("/app/data/covers_thumb")
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f"{book_id}.jpg"

    if not thumb_path.exists():
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_W, THUMB_W * 3), Image.LANCZOS)
            img.save(thumb_path, "JPEG", quality=72, optimize=True)

    resp = FileResponse(thumb_path, media_type="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=604800"  # 7 days
    return resp


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


def main():
    """Entry point for CLI."""
    import uvicorn
    uvicorn.run(
        "greatreads.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
