"""Detect low-resolution covers and upgrade them from Apple Books hi-res (#165).

Apple's iTunes artwork upscales to ~3000px, so a small stored cover (e.g. a 128px
Google thumbnail from an old import) can usually be replaced with a much sharper one.
Backfill-style + non-destructive: a cover is only overwritten when Apple actually
returns a HIGHER-resolution image. User-triggered from Settings (bounded per run so
it doesn't hammer Apple)."""

import logging
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image
from sqlalchemy.orm import Session

from ..config import settings
from ..models.book import Book
from ..discovery.itunes_client import ITunesClient

logger = logging.getLogger(__name__)

LOW_RES_WIDTH = 400   # covers narrower than this (px) are upgrade candidates
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36 GreatReads/cover-fetch")
_THUMB_DIR = Path("/app/data/covers_thumb")


def _width(path: Path):
    try:
        with Image.open(path) as im:
            return im.size[0]
    except Exception:
        return None


def upgrade_low_res(db: Session, limit: int = 25, min_width: int = LOW_RES_WIDTH) -> dict:
    """Scan stored covers; for up to ``limit`` that are below ``min_width`` px wide, try
    an Apple hi-res replacement and swap it in when it's genuinely larger."""
    covers_dir = settings.covers_dir
    itc = ITunesClient()
    processed = upgraded = 0
    books = (db.query(Book)
             .filter(Book.cover.is_(True), Book.title.isnot(None))
             .order_by(Book.id).all())
    for b in books:
        if processed >= limit:
            break
        path = covers_dir / f"{b.id}.jpg"
        if not path.exists():
            continue
        cur_w = _width(path)
        if cur_w is None or cur_w >= min_width:
            continue
        processed += 1
        try:
            url = itc.cover_by_title_author(b.title, b.author or "")
        except Exception:
            url = None
        if not url:
            continue
        try:
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
            if r.status_code != 200 or not r.headers.get("content-type", "").lower().startswith("image/"):
                continue
            new_w = Image.open(BytesIO(r.content)).size[0]
        except Exception:
            continue
        if new_w > cur_w:
            try:
                path.write_bytes(r.content)
                # Drop the cached thumbnail so it regenerates from the sharper cover.
                (_THUMB_DIR / f"{b.id}.jpg").unlink(missing_ok=True)
                upgraded += 1
                try:
                    from .event_log_service import log_event
                    log_event("cover", "upgraded", level="success", book_id=b.id, title=b.title,
                              detail={"from_px": cur_w, "to_px": new_w})
                except Exception:
                    pass
            except Exception:
                pass
    return {"processed_lowres": processed, "upgraded": upgraded, "min_width": min_width}
