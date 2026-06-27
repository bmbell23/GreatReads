"""Format-dominance engine (#67).

Derives a book's "primary" format from reading-session activity instead of pinning
it to a single declared media. The metric is **cumulative word-equivalents per
format** (audio words are word-equivalents); the primary is simply the format with
the most cumulative words — so it only swaps when one format genuinely overtakes
another (no decay / hysteresis needed). Physical competes via manual-progress words
(`phys:<reading_id>` keys in reading_activity, see #70).

Phase 1: read-only derivation. Later phases feed this into WPD/estimated-finish, the
per-format TBR chain heads, and Home card theming.
"""

from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# Canonical format names as stored in reading_activity.format.
FORMATS = ("Ebook", "Audio", "Physical")

# Deterministic tiebreak order when two formats have equal cumulative words.
_TIE_ORDER = {"Ebook": 0, "Audio": 1, "Physical": 2}

# Map declared reading.media (incl. legacy values) to a canonical format.
_MEDIA_TO_FORMAT = {
    "ebook": "Ebook",
    "audio": "Audio",
    "audiobook": "Audio",
    "physical": "Physical",
    "hardcover": "Physical",
    "paperback": "Physical",
}


def normalize_format(media: Optional[str]) -> Optional[str]:
    """Normalize a declared media string to a canonical format, or None."""
    if not media:
        return None
    return _MEDIA_TO_FORMAT.get(media.strip().lower(), media.strip().title())


def get_format_words(db: Session, book_id: int) -> Dict[str, int]:
    """Cumulative word-equivalents logged per format for a book.

    Sums reading_activity.words across all of the book's activity keys: its
    Calibre id / ``abs:<id>`` (via external_imports) AND ``phys:<reading_id>``
    for every reading of the book (physical, which has no ereader sessions).
    """
    try:
        rows = db.execute(text(
            "SELECT ra.format, COALESCE(SUM(ra.words), 0) AS words "
            "FROM reading_activity ra WHERE ra.book_key IN ("
            "  SELECT CASE WHEN ei.source='audiobookshelf' THEN 'abs:' || ei.external_id "
            "              ELSE ei.external_id END "
            "  FROM external_imports ei WHERE ei.book_id = :bid "
            "  UNION "
            "  SELECT 'phys:' || r.id FROM read r WHERE r.book_id = :bid) "
            "GROUP BY ra.format"
        ), {"bid": book_id}).fetchall()
    except Exception:
        return {}
    return {fmt: int(words or 0) for fmt, words in rows if fmt}


def get_format_split(db: Session, book_id: int) -> Dict[str, Dict[str, float]]:
    """Per-format share of cumulative words: {fmt: {"words": int, "pct": 0..1}}."""
    words = get_format_words(db, book_id)
    total = sum(words.values())
    return {
        fmt: {"words": w, "pct": (w / total if total else 0.0)}
        for fmt, w in words.items()
    }


def get_primary_format(
    db: Session, book_id: int, fallback_media: Optional[str] = None
) -> Optional[str]:
    """The book's dominant format = most cumulative words.

    Falls back to the normalized declared media when no activity is logged yet
    (e.g. a freshly-started reading, or a physical book before any progress).
    Ties break deterministically by FORMATS order so the result is stable.
    """
    words = get_format_words(db, book_id)
    if not words or all(v <= 0 for v in words.values()):
        return normalize_format(fallback_media)
    # Highest words wins; tiebreak by canonical order (lower index preferred).
    return max(
        words.items(),
        key=lambda kv: (kv[1], -_TIE_ORDER.get(kv[0], 99)),
    )[0]
