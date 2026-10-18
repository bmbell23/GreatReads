"""Settings service for retrieving user settings."""

import json
import math
from datetime import date, timedelta
from typing import Dict, Optional
from sqlalchemy.orm import Session

from ..models.user_settings import UserSettings

# Default reading speeds (words per day) by media type
DEFAULT_READING_SPEEDS = {
    "ebook": 15000,
    "physical": 12000,
    "hardcover": 12000,
    "audio": 25000,
    "audiobook": 25000,
}
DEFAULT_WPD = 12000

# Exponential decay rate per month for recency weighting.
# At 0.5/month: weight after 1mo ≈ 61%, 3mo ≈ 22%, 6mo ≈ 5%, 12mo ≈ 0.2%.
# Books from the last 1-3 months dominate; 6+ months contribute very little.
_DECAY_RATE_PER_MONTH = 0.5


def get_wpd_mode(db: Session) -> str:
    """Return current WPD calculation mode: 'manual' or 'auto'."""
    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == "wpd_mode"
    ).first()
    return setting.setting_value if setting else "manual"


_FORMATS = ("ebook", "physical", "audio")
_ACTIVITY_FORMAT_MAP = {"ebook": "ebook", "physical": "physical", "audio": "audio"}

# #369 blend parameters
_RECENT_WINDOW_DAYS = 90
_RECENT_HALF_LIFE_DAYS = 14.0
_HIST_HALF_LIFE_DAYS = 120.0
_TRACK_START = date(2026, 6, 19)  # reading_activity tracking began
_MAX_RECENT_WEIGHT = 0.7
_ACTIVITY_DAYS_WINDOW = 60
_ACTIVITY_DAYS_FULL = 30

# In-process cache of the breakdown, keyed by local date (#369).
_auto_cache: Dict[date, Dict[str, Dict[str, Optional[float]]]] = {}


def invalidate_auto_wpd_cache() -> None:
    """Drop cached auto-WPD results (call after reading activity is written)."""
    _auto_cache.clear()


def _media_to_format(media: Optional[str]) -> Optional[str]:
    media = (media or "").lower()
    if media in ("ebook", "kindle"):
        return "ebook"
    if media in ("physical", "hardcover", "paperback"):
        return "physical"
    if media in ("audio", "audiobook"):
        return "audio"
    return None


def _historical_signal(db: Session, today: date) -> Dict[str, Dict[str, Optional[float]]]:
    """Weighted finished-book words/day per format (recency x sqrt(duration))."""
    from ..models.reading import Reading

    finished = db.query(Reading).filter(
        Reading.date_finished_actual.isnot(None),
        Reading.date_started.isnot(None),
    ).all()

    acc = {f: [0.0, 0.0, 0] for f in _FORMATS}  # sum(w*wpd), sum(w), n
    for r in finished:
        if not r.book or not r.book.word_count:
            continue
        days = (r.date_finished_actual - r.date_started).days + 1
        if days <= 0:
            continue
        fmt = _media_to_format(r.media)
        if fmt is None:
            continue
        wpd = r.book.word_count / days
        age = max(0, (today - r.date_finished_actual).days)
        w = math.pow(0.5, age / _HIST_HALF_LIFE_DAYS) * math.sqrt(days)
        acc[fmt][0] += wpd * w
        acc[fmt][1] += w
        acc[fmt][2] += 1
    return {
        f: {"historical": (v[0] / v[1]) if v[1] > 0 else None, "books_used": v[2]}
        for f, v in acc.items()
    }


def _recent_signal(db: Session, today: date) -> Dict[str, Dict[str, Optional[float]]]:
    """Exponentially-weighted words per calendar day from reading_activity."""
    from sqlalchemy import text

    out: Dict[str, Dict[str, Optional[float]]] = {
        f: {"recent": None, "tracked_days": 0} for f in _FORMATS
    }
    try:
        window_start = today - timedelta(days=_RECENT_WINDOW_DAYS - 1)
        rows = db.execute(text(
            "SELECT activity_date, format, SUM(words) AS w, SUM(minutes) AS m "
            "FROM reading_activity WHERE activity_date >= :s AND activity_date <= :e "
            "GROUP BY activity_date, format"
        ), {"s": window_start.isoformat(), "e": today.isoformat()}).fetchall()
        earliest_row = db.execute(text("SELECT MIN(activity_date) FROM reading_activity")).scalar()
    except Exception:
        return out  # table missing / unreadable: no recent signal

    track_start = _TRACK_START
    if earliest_row:
        try:
            track_start = min(track_start, date.fromisoformat(str(earliest_row)[:10]))
        except ValueError:
            pass
    start = max(window_start, track_start)

    words_by: Dict[str, Dict[date, float]] = {f: {} for f in _FORMATS}
    active_by: Dict[str, set] = {f: set() for f in _FORMATS}
    for d, fmt_raw, w, m in rows:
        fmt = _ACTIVITY_FORMAT_MAP.get(str(fmt_raw or "").lower())
        if fmt is None:
            continue
        try:
            day = date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        words_by[fmt][day] = words_by[fmt].get(day, 0.0) + float(w or 0)
        if (w or 0) > 0 or (m or 0) > 0:
            active_by[fmt].add(day)

    if start > today:
        return out
    n_days = (today - start).days + 1
    act_cutoff = today - timedelta(days=_ACTIVITY_DAYS_WINDOW - 1)
    for fmt in _FORMATS:
        wsum = vsum = 0.0
        for i in range(n_days):
            day = start + timedelta(days=i)
            w = math.pow(0.5, (today - day).days / _RECENT_HALF_LIFE_DAYS)
            wsum += w
            vsum += w * words_by[fmt].get(day, 0.0)
        active = sum(1 for d in active_by[fmt] if d >= act_cutoff)
        # Only a recent signal if this format has any activity at all in the window.
        has_any = bool(active_by[fmt])
        out[fmt] = {
            "recent": (vsum / wsum) if (wsum > 0 and has_any) else None,
            "tracked_days": active,
        }
    return out


def calculate_auto_wpd_breakdown(db: Session) -> Dict[str, Dict[str, Optional[float]]]:
    """Per-format blend of recent daily reading and weighted history (#369).

    Each format maps to {recent, historical, weight_recent, wpd, tracked_days,
    books_used}. wpd is an int, or None when neither signal exists.
    Cached per local date until invalidate_auto_wpd_cache().
    """
    today = date.today()
    cached = _auto_cache.get(today)
    if cached is not None:
        return cached

    hist = _historical_signal(db, today)
    rec = _recent_signal(db, today)
    result: Dict[str, Dict[str, Optional[float]]] = {}
    for fmt in _FORMATS:
        recent = rec[fmt]["recent"]
        historical = hist[fmt]["historical"]
        tracked = int(rec[fmt]["tracked_days"] or 0)
        a = _MAX_RECENT_WEIGHT * min(1.0, tracked / _ACTIVITY_DAYS_FULL)
        if recent is not None and historical is not None:
            wpd = a * recent + (1 - a) * historical
        elif recent is not None:
            wpd, a = recent, 1.0
        elif historical is not None:
            wpd, a = historical, 0.0
        else:
            wpd, a = None, 0.0
        result[fmt] = {
            "recent": round(recent) if recent is not None else None,
            "historical": round(historical) if historical is not None else None,
            "weight_recent": round(a, 3),
            "wpd": round(wpd) if wpd is not None else None,
            "tracked_days": tracked,
            "books_used": hist[fmt]["books_used"],
        }
    _auto_cache.clear()
    _auto_cache[today] = result
    return result


def calculate_auto_wpd(db: Session) -> Dict[str, Optional[int]]:
    """Auto WPD per format ('ebook', 'physical', 'audio'); None if no data.

    Blends exponentially-weighted recent daily reading (reading_activity) with
    duration/recency-weighted finished-book history. See #369.
    """
    breakdown = calculate_auto_wpd_breakdown(db)
    return {fmt: breakdown[fmt]["wpd"] for fmt in _FORMATS}


def _get_manual_speeds(db: Session) -> Dict[str, int]:
    """Return manually configured reading speeds (or defaults)."""
    setting = db.query(UserSettings).filter(
        UserSettings.setting_key == "reading_speeds"
    ).first()

    if setting:
        try:
            speeds = json.loads(setting.setting_value)
            return {
                "ebook": speeds.get("ebook_wpd", DEFAULT_READING_SPEEDS["ebook"]),
                "physical": speeds.get("physical_wpd", DEFAULT_READING_SPEEDS["physical"]),
                "hardcover": speeds.get("physical_wpd", DEFAULT_READING_SPEEDS["hardcover"]),
                "audio": speeds.get("audio_wpd", DEFAULT_READING_SPEEDS["audio"]),
                "audiobook": speeds.get("audio_wpd", DEFAULT_READING_SPEEDS["audiobook"]),
            }
        except (json.JSONDecodeError, KeyError):
            pass

    return dict(DEFAULT_READING_SPEEDS)


def get_reading_speeds(db: Session) -> Dict[str, int]:
    """Get reading speeds (WPD) respecting the current wpd_mode setting.

    In 'auto' mode the weighted historical average is used, falling back
    to manual/default values for formats that have no historical data.
    In 'manual' mode the user-configured (or default) values are used.
    """
    mode = get_wpd_mode(db)

    if mode == "auto":
        auto = calculate_auto_wpd(db)
        manual = _get_manual_speeds(db)
        ebook_wpd = auto.get("ebook") or manual["ebook"]
        physical_wpd = auto.get("physical") or manual["physical"]
        audio_wpd = auto.get("audio") or manual["audio"]
        return {
            "ebook": ebook_wpd,
            "physical": physical_wpd,
            "hardcover": physical_wpd,
            "audio": audio_wpd,
            "audiobook": audio_wpd,
        }

    return _get_manual_speeds(db)


def get_wpd_for_media(db: Session, media_type: str) -> int:
    """Get words per day for a specific media type."""
    speeds = get_reading_speeds(db)
    return speeds.get(media_type.lower(), DEFAULT_WPD)

