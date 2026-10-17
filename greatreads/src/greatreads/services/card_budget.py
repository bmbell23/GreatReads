"""Shared library-card borrow budget (#357).

Why: OverDrive rate-limited our cards ("too many titles borrowed and returned from your
account within a short period of time ... try again in several days") after 124 automatic
borrows in 3 days on ONE card, picked every time because it had the most loan headroom,
each returned instantly. Both automatic borrowers -- wishlist auto-acquire (#297/#349) and
hold auto-fulfill (#179) -- now go through this module:

  * ``can_borrow`` / ``check``: a rolling per-card limit per day (default 2) and per 7 days
    (default 6), a global per-day cap across all cards (default 10), and no active cooldown.
  * ``pick_card``: among the cards that pass, the LEAST RECENTLY borrowed (never headroom).
  * ``record_borrow``: every automatic borrow, persistently (``card_borrows`` table).
  * ``cooldown``: park a card (default 7 days) -- used when OverDrive refuses (``is_rate_limit``).
  * ``min_loan_days``: how old a loan must be before #179's tail auto-returns it.

All limits live in user_settings (defaults below, editable in Settings). "Day" is a rolling
24 h, "week" a rolling 7 d, in UTC like every other timestamp here.

History seed: the first time the budget is consulted it backfills ``card_borrows`` ONCE from
the event_log (libby borrow / auto_borrow / wishlist_acquire-acquired events of the last 7
days that carry a card_id), then sets the ``card_budget_seeded`` setting so it never repeats
(no double counting with later ``record_borrow`` calls). Note: manual UI borrows logged
before #357 carry only a library name, not a card_id, so they are not seeded.
"""

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy.orm import Session

from ..models.card_borrow import CardBorrow
from ..models.event_log import EventLog
from ..models.user_settings import UserSettings
from .event_log_service import log_event

logger = logging.getLogger(__name__)

SETTING_PER_DAY = "card_budget_per_day"
SETTING_PER_WEEK = "card_budget_per_week"
SETTING_GLOBAL_PER_DAY = "card_budget_global_per_day"
SETTING_MIN_LOAN_DAYS = "card_budget_min_loan_days"
SETTING_COOLDOWNS = "card_budget_cooldowns"       # JSON {card_id: {until, reason, library}}
SETTING_SEEDED = "card_budget_seeded"

DEFAULT_PER_DAY = 2
DEFAULT_PER_WEEK = 6
DEFAULT_GLOBAL_PER_DAY = 10
DEFAULT_MIN_LOAN_DAYS = 3
DEFAULT_COOLDOWN_DAYS = 7

DAY = timedelta(days=1)
WEEK = timedelta(days=7)

# OverDrive's refusal text (#357). Case-insensitive; either phrase is enough.
_RATE_LIMIT_RE = re.compile(
    r"too\s+many\s+titles\s+borrowed\s+and\s+returned|try\s+again\s+in\s+several\s+days", re.I)

_SEED_LOCK = threading.Lock()


# ── settings ──────────────────────────────────────────────────────────────────

def _get(db: Session, key: str, default=None):
    s = db.query(UserSettings).filter(UserSettings.setting_key == key).first()
    return s.setting_value if (s and s.setting_value is not None) else default


def _set(db: Session, key: str, value) -> None:
    s = db.query(UserSettings).filter(UserSettings.setting_key == key).first()
    if s:
        s.setting_value = str(value)
    else:
        db.add(UserSettings(setting_key=key, setting_value=str(value)))
    db.commit()


def _get_num(db: Session, key: str, default, cast=int):
    try:
        v = cast(_get(db, key, default))
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


def limits(db: Session) -> dict:
    return {
        "per_day": _get_num(db, SETTING_PER_DAY, DEFAULT_PER_DAY),
        "per_week": _get_num(db, SETTING_PER_WEEK, DEFAULT_PER_WEEK),
        "global_per_day": _get_num(db, SETTING_GLOBAL_PER_DAY, DEFAULT_GLOBAL_PER_DAY),
        "min_loan_days": _get_num(db, SETTING_MIN_LOAN_DAYS, DEFAULT_MIN_LOAN_DAYS, float),
    }


def set_limits(db: Session, *, per_day=None, per_week=None, global_per_day=None,
               min_loan_days=None) -> dict:
    for key, val in ((SETTING_PER_DAY, per_day), (SETTING_PER_WEEK, per_week),
                     (SETTING_GLOBAL_PER_DAY, global_per_day)):
        if val is not None:
            _set(db, key, max(0, int(val)))
    if min_loan_days is not None:
        _set(db, SETTING_MIN_LOAN_DAYS, max(0.0, float(min_loan_days)))
    return limits(db)


def min_loan_days(db: Session) -> float:
    return limits(db)["min_loan_days"]


# ── seeding ───────────────────────────────────────────────────────────────────

def _ensure_table(db: Session) -> None:
    CardBorrow.__table__.create(bind=db.get_bind(), checkfirst=True)


def _seed_rows(db: Session, now: datetime) -> list:
    """(card_id, at, title) for every counted borrow event of the last 7 days."""
    since = now - WEEK
    q = (db.query(EventLog)
         .filter(EventLog.category == "libby",
                 EventLog.event.in_(("borrow", "auto_borrow", "wishlist_acquire")),
                 EventLog.level.in_(("success", "info")),
                 EventLog.ts >= since))
    out = []
    for e in q.all():
        try:
            d = json.loads(e.detail) if e.detail else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        cid = str(d.get("card_id") or "").strip()
        if not cid:
            continue
        if e.event == "wishlist_acquire":
            # Only a REAL new borrow: status acquired and acted (older rows lack `acted`:
            # fall back to the reason text; "already on loan" rows are not borrows).
            if d.get("status") != "acquired":
                continue
            acted = d.get("acted")
            if acted is None:
                if not str(d.get("reason") or "").lower().startswith("borrowed"):
                    continue
            elif not acted:
                continue
        elif e.level != "success":
            continue
        out.append((cid, e.ts, e.title))
    return out


def ensure_seeded(db: Session, now: Optional[datetime] = None) -> None:
    """Create the table if needed and backfill history from event_log, once."""
    with _SEED_LOCK:
        _ensure_table(db)
        if _get(db, SETTING_SEEDED):
            return
        now = now or datetime.utcnow()
        for cid, at, title in _seed_rows(db, now):
            db.add(CardBorrow(card_id=cid, at=at, source="seed", title=title))
        db.commit()
        _set(db, SETTING_SEEDED, now.isoformat())


def seed_counts(db: Session) -> dict:
    """{card_id: n} of the seeded rows (diagnostic)."""
    ensure_seeded(db)
    out: dict = {}
    for (cid,) in db.query(CardBorrow.card_id).filter(CardBorrow.source == "seed").all():
        out[cid] = out.get(cid, 0) + 1
    return out


# ── cooldowns ─────────────────────────────────────────────────────────────────

def _cooldowns(db: Session) -> dict:
    raw = _get(db, SETTING_COOLDOWNS)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def cooldown_until(db: Session, card_id: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """End of an ACTIVE cooldown for the card, else None."""
    now = now or datetime.utcnow()
    c = _cooldowns(db).get(str(card_id))
    if not c:
        return None
    try:
        until = datetime.fromisoformat(c.get("until", ""))
    except (TypeError, ValueError):
        return None
    return until if until > now else None


def cooldown(db: Session, card_id: str, days: float = DEFAULT_COOLDOWN_DAYS, reason: str = "",
             library: str = "", now: Optional[datetime] = None) -> datetime:
    """Park a card until now + days. Returns the end time."""
    now = now or datetime.utcnow()
    until = now + timedelta(days=days)
    cds = _cooldowns(db)
    cds[str(card_id)] = {"until": until.isoformat(), "reason": (reason or "")[:300],
                         "library": library or "", "set_at": now.isoformat()}
    # Drop expired entries while we're here.
    cds = {k: v for k, v in cds.items() if _alive(v, now)}
    _set(db, SETTING_COOLDOWNS, json.dumps(cds))
    return until


def clear_cooldown(db: Session, card_id: str) -> None:
    cds = _cooldowns(db)
    if cds.pop(str(card_id), None) is not None:
        _set(db, SETTING_COOLDOWNS, json.dumps(cds))


def _alive(c: dict, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(c.get("until", "")) > now
    except (TypeError, ValueError):
        return False


def is_rate_limit(message) -> bool:
    """True if a borrow error is OverDrive's 'too many titles borrowed and returned' refusal."""
    return bool(message) and bool(_RATE_LIMIT_RE.search(str(message)))


def handle_rate_limit(db: Session, card_id: str, library: str, message: str,
                      title: str = "", days: float = DEFAULT_COOLDOWN_DAYS) -> datetime:
    """Cool the card down and log the libby/card_cooldown warning (#357)."""
    until = cooldown(db, card_id, days=days, reason=str(message), library=library)
    log_event("libby", "card_cooldown", level="warn", title=title or None,
              detail={"card_id": str(card_id), "library": library, "message": str(message)[:500],
                      "until": until.isoformat(), "days": days})
    return until


# ── history + checks ──────────────────────────────────────────────────────────

def _times(db: Session, now: datetime, card_id: Optional[str] = None) -> list:
    """Ascending borrow timestamps within the last 7 days (optionally one card)."""
    q = db.query(CardBorrow.at).filter(CardBorrow.at > now - WEEK)
    if card_id is not None:
        q = q.filter(CardBorrow.card_id == str(card_id))
    return sorted(r[0] for r in q.all())


def _free_time(times: list, window: timedelta, limit: int, now: datetime) -> datetime:
    """Earliest moment a rolling ``limit``-per-``window`` cap has room (``now`` if it has)."""
    if limit <= 0:
        return now + window
    inwin = [t for t in times if t > now - window]
    if len(inwin) < limit:
        return now
    return inwin[len(inwin) - limit] + window


def check(db: Session, card_id: str, now: Optional[datetime] = None) -> tuple:
    """(ok, reason, free_at). ``free_at`` is when this card will next have budget (== now if ok)."""
    now = now or datetime.utcnow()
    ensure_seeded(db)
    lim = limits(db)
    cid = str(card_id)
    until = cooldown_until(db, cid, now)
    card_t = _times(db, now, cid)
    all_t = _times(db, now)
    free = now
    why = []
    if until:
        free = max(free, until)
        why.append(f"cooldown until {until.isoformat(timespec='minutes')}")
    f = _free_time(card_t, DAY, lim["per_day"], now)
    if f > now:
        free = max(free, f)
        why.append(f"card at its {lim['per_day']}/day limit")
    f = _free_time(card_t, WEEK, lim["per_week"], now)
    if f > now:
        free = max(free, f)
        why.append(f"card at its {lim['per_week']}/week limit")
    f = _free_time(all_t, DAY, lim["global_per_day"], now)
    if f > now:
        free = max(free, f)
        why.append(f"global {lim['global_per_day']}/day cap reached")
    return (not why, "; ".join(why), free)


def can_borrow(db: Session, card_id: str, now: Optional[datetime] = None) -> bool:
    return check(db, card_id, now)[0]


def last_borrowed(db: Session, card_id: str) -> Optional[datetime]:
    return db.query(CardBorrow.at).filter(CardBorrow.card_id == str(card_id)) \
        .order_by(CardBorrow.at.desc()).limit(1).scalar()


def pick_card(db: Session, candidates: list, card_of: Callable = lambda c: c["cardId"],
              now: Optional[datetime] = None):
    """Among ``candidates`` whose card passes ``can_borrow``, the LEAST RECENTLY borrowed
    one (a never-borrowed card first; ties: fewer borrows this week, then input order).
    Loan headroom is deliberately NOT a criterion. None if no card has budget."""
    now = now or datetime.utcnow()
    best, best_key = None, None
    for i, c in enumerate(candidates):
        cid = str(card_of(c))
        if not can_borrow(db, cid, now):
            continue
        last = last_borrowed(db, cid)
        key = (last or datetime.min, len(_times(db, now, cid)), i)
        if best_key is None or key < best_key:
            best, best_key = c, key
    return best


def earliest_free(db: Session, card_ids, now: Optional[datetime] = None) -> datetime:
    """When the first of ``card_ids`` will have budget again."""
    now = now or datetime.utcnow()
    frees = [check(db, c, now)[2] for c in card_ids]
    return min(frees) if frees else now


def record_borrow(db: Session, card_id: str, title: str = "", source: str = "",
                  now: Optional[datetime] = None) -> None:
    ensure_seeded(db)
    db.add(CardBorrow(card_id=str(card_id), at=now or datetime.utcnow(),
                      source=source or None, title=(title or None)))
    db.commit()


# ── Settings card data ────────────────────────────────────────────────────────

def status(db: Session, cards: Optional[list] = None, now: Optional[datetime] = None) -> dict:
    """Limits + a per-card table. ``cards`` = the engine's /api/cards/status list (for names);
    cards we have history/cooldowns for but the engine did not list are included too."""
    now = now or datetime.utcnow()
    ensure_seeded(db)
    names = {}
    for c in cards or []:
        cid = str(c.get("cardId", ""))
        if cid:
            names[cid] = {"card": c.get("cardLabel") or cid, "library": c.get("name") or ""}
    cds = _cooldowns(db)
    ids = list(names)
    for cid in {r[0] for r in db.query(CardBorrow.card_id).filter(CardBorrow.at > now - WEEK).all()} | set(cds):
        if cid not in names:
            names[cid] = {"card": cid, "library": (cds.get(cid) or {}).get("library", "")}
            ids.append(cid)
    rows = []
    for cid in ids:
        t = _times(db, now, cid)
        until = cooldown_until(db, cid, now)
        ok, why, free = check(db, cid, now)
        rows.append({
            "card_id": cid, **names[cid],
            "today": sum(1 for x in t if x > now - DAY), "week": len(t),
            "cooldown_until": until.isoformat() if until else None,
            "cooldown_reason": (cds.get(cid) or {}).get("reason") if until else None,
            "can_borrow": ok, "why": why,
            "free_at": free.isoformat() if not ok else None,
        })
    all_t = _times(db, now)
    return {"limits": limits(db), "global_today": sum(1 for x in all_t if x > now - DAY),
            "cards": rows}
