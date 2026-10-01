"""MediaForge handoff (#326): ask for one audiobook's chapters + sync map, then
post what MediaForge did into the book's Bookworm thread.

The contract is MediaForge's (``docs/PIPELINE.md`` §12, MediaForge #30): files on
the share, because neither container can call the other.

- **Ask:** when a new ABS item lands in GreatReads, write
  ``requests/<abs_id>.json`` = ``{"abs_id", "title", "requested_at"}`` (tmp + rename).
- **Answer:** MediaForge's ``*/10`` worker writes ``requests/<abs_id>.done.json``
  with a list of ``events`` (``{"event", "detail"}``). :func:`poll_answers` logs
  each as ``log_event("media", …)``, which Bookworm posts reply-only, then deletes
  the answer.

Best-effort: a missing/read-only requests dir just means no request and no answer.
"""

import json
import logging
import os
from datetime import datetime, timezone
from glob import glob

logger = logging.getLogger(__name__)

REQUESTS_DIR = os.environ.get("MEDIAFORGE_REQUESTS_DIR", "/media/sync-maps/requests")

# event → log level; anything else MediaForge adds later logs as info.
LEVELS = {
    "chapters_fixed": "success",
    "sync_map_built": "success",
    "chapters_parked": "warning",
    "sync_map_failed": "warning",
}


def _safe_id(abs_id) -> str:
    """An ABS id is a uuid; refuse anything that could walk out of the folder."""
    s = str(abs_id or "").strip()
    return s if s and all(c.isalnum() or c in "-_" for c in s) else ""


def request_sync(abs_id, title: str) -> bool:
    """Queue one ABS item for MediaForge. Never raises."""
    aid = _safe_id(abs_id)
    if not aid or not os.path.isdir(REQUESTS_DIR):
        return False
    path = os.path.join(REQUESTS_DIR, f"{aid}.json")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"abs_id": aid, "title": title or "",
                       "requested_at": datetime.now(timezone.utc).isoformat()}, f)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("mediaforge request for %s failed: %s", aid, exc)
        return False


def _book_for(db, abs_id):
    """(book_id, GreatReads title) for an ABS item, so the line lands in the same
    thread as the borrow/import lines even when ABS spells the title differently."""
    from ..models.book import Book
    from ..models.external_import import ExternalImport
    rec = (db.query(ExternalImport)
           .filter(ExternalImport.source == "audiobookshelf", ExternalImport.external_id == abs_id)
           .first())
    book = db.get(Book, rec.book_id) if rec else None
    return (book.id, book.title) if book else (None, None)


def poll_answers(db) -> int:
    """Log every finished MediaForge answer and delete it. Returns answers handled."""
    from .event_log_service import log_event
    handled = 0
    for path in sorted(glob(os.path.join(REQUESTS_DIR, "*.done.json"))):
        try:
            with open(path) as f:
                ans = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("mediaforge answer %s unreadable: %s", path, exc)
            ans = None
        if isinstance(ans, dict):
            abs_id = _safe_id(ans.get("abs_id")) or os.path.basename(path)[:-len(".done.json")]
            book_id, title = _book_for(db, abs_id)
            title = title or ans.get("title") or None
            events = [e for e in (ans.get("events") or []) if isinstance(e, dict) and e.get("event")]
            if not events and ans.get("status") == "unknown":
                events = [{"event": "unknown", "detail": ans.get("reason") or ""}]
            for ev in events:
                name = str(ev["event"])[:100]
                log_event("media", name, level=LEVELS.get(name, "info"), book_id=book_id, title=title,
                          detail={"abs_id": abs_id, "text": str(ev.get("detail") or "")[:300]})
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning("mediaforge answer %s not deleted: %s", path, exc)
            continue
        handled += 1
    return handled
