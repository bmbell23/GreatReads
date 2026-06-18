"""Main FastAPI application."""

import logging
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
from .routes import books, readings, chains, library, settings as settings_routes, stats, auth, inventory, reports, import_routes, bookshelves
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


# How often to poll Calibre/ABS for new items (minutes).
AUTO_SYNC_INTERVAL_MINUTES = 15


def _auto_sync():
    """Scheduled job: import new Calibre/ABS items into GreatReads."""
    db = SessionLocal()
    try:
        from .services.sync_service import sync_all
        sync_all(db)
    except Exception as exc:
        logger.error("Auto-sync failed: %s", exc)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    create_tables()

    # Background schedulers are opt-out so a vendored/second copy of GreatReads
    # (e.g. the isolated :8092 instance inside the Ereader repo) doesn't auto-sync
    # or write the DB behind our back. Default stays enabled for production.
    import os
    if os.environ.get("ENABLE_SCHEDULERS", "true").lower() != "true":
        logger.info("Schedulers disabled (ENABLE_SCHEDULERS=false); skipping background jobs.")
        yield
        return

    # ── Midnight chain-recalculation scheduler (Mountain Time) ──────────
    try:
        from datetime import datetime
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
        scheduler.start()
        logger.info(
            "Schedulers started: midnight chain-recalc + auto-sync every %d min.",
            AUTO_SYNC_INTERVAL_MINUTES,
        )
    except Exception as exc:
        logger.error("Failed to start scheduler: %s", exc)
        scheduler = None

    yield

    # Shutdown
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


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


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    """User settings page."""
    current_user = get_current_user_from_cookie(request, db)
    return templates.TemplateResponse(request, "settings.html", {"current_user": current_user})


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
