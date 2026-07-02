"""Auto-sync service: keep GreatReads in step with Calibre and ABS.

Polls both external libraries for items not yet imported and imports them:
  - high-confidence title/author matches are LINKED onto the existing book
    (so a Calibre ebook and its ABS audiobook collapse into one dual-format
    book, and ownership flags are set on the shared inventory row)
  - everything else is CREATED as a new book

Both external DBs are read-only; this only writes to GreatReads. Calibre runs
first so ABS matching can see freshly-created ebooks within the same run.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from .import_service import (
    get_calibre_candidates,
    get_abs_candidates,
    import_calibre_book,
    import_abs_book,
    refresh_calibre_metadata,
)

logger = logging.getLogger(__name__)

# Minimum _best_match score to auto-link onto an existing book rather than
# create a new one. Borderline matches are left for manual linking in the UI.
AUTO_LINK_THRESHOLD = 0.9


def _match_book_id(candidate: dict) -> int | None:
    """Return the existing book id to link onto, or None to create new."""
    match = candidate.get("similar_to")
    if match and match.get("score", 0) >= AUTO_LINK_THRESHOLD:
        return match["id"]
    return None


def sync_all(db: Session) -> dict[str, Any]:
    """Import every not-yet-imported Calibre and ABS item.

    Returns a summary dict of counts. Per-item failures are rolled back,
    logged, and skipped so one bad record can't abort the whole run.
    """
    summary = {
        "calibre_created": 0, "calibre_linked": 0, "calibre_failed": 0,
        "abs_created": 0, "abs_linked": 0, "abs_failed": 0,
    }

    # --- Calibre first (creates ebooks ABS can later match against) ---
    for cand in get_calibre_candidates(db):
        cal_id = cand["external_id"]
        book_id = _match_book_id(cand)
        try:
            import_calibre_book(db, cal_id, book_id=book_id)
            summary["calibre_linked" if book_id else "calibre_created"] += 1
        except Exception as exc:
            db.rollback()
            summary["calibre_failed"] += 1
            logger.warning("Auto-sync: Calibre %s failed: %s", cal_id, exc)

    # --- ABS second (multi-part groups already collapsed by the candidate fn,
    #     and matching now sees ebooks created above) ---
    for cand in get_abs_candidates(db):
        abs_id = cand["external_id"]
        book_id = _match_book_id(cand)
        try:
            import_abs_book(db, abs_id, book_id=book_id)
            summary["abs_linked" if book_id else "abs_created"] += 1
        except Exception as exc:
            db.rollback()
            summary["abs_failed"] += 1
            logger.warning("Auto-sync: ABS %s failed: %s", abs_id, exc)

    # --- Backfill empty metadata from Calibre for already-linked books (#147) —
    #     self-heals sparse snapshots (missing word count / series / date) when
    #     Calibre's metadata is completed after the initial import.
    try:
        refreshed = refresh_calibre_metadata(db)
        summary["calibre_refreshed"] = refreshed.get("updated", 0)
    except Exception as exc:
        db.rollback()
        summary["calibre_refreshed"] = 0
        logger.warning("Auto-sync: Calibre metadata refresh failed: %s", exc)

    total = (summary["calibre_created"] + summary["calibre_linked"]
             + summary["abs_created"] + summary["abs_linked"])
    if total or summary["calibre_failed"] or summary["abs_failed"] or summary.get("calibre_refreshed"):
        logger.info("Auto-sync complete: %s", summary)
    return summary
