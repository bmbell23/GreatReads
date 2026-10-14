"""Wishlist auto-acquire (#297).

Every N minutes, take ONE Wishlist book and, per format, reach exactly one outcome:

  (a) available on a usable card  -> borrow + download, hand the loan to #179's
      confirm-import -> return tail (``enqueue_borrowed``);
  (b) not available               -> place a hold on the card with the most hold
      headroom (never past HALF that card's hold limit; #179 claims it when ready);
  (c) neither possible            -> record WHY, with a backoff, so it is never retried
      blindly (the #282 lesson: a sweep without a cursor re-picks the queue head forever).

Decisions (#297, resolved 2026-10-14):
  * ebook first; audiobook only on cards that can harvest (the prbn:v chip, #296/#298 —
    the engine's per-card ``can_harvest`` flag on /api/cards/status);
  * automatic holds are capped at half of each card's hold limit;
  * order: books in a series with an in-progress or finished reading first, then the
    highest book id (most recently wishlisted);
  * loans are returned after a CONFIRMED import (ebook and audiobook) by #179's tail.

Opt-in, default OFF (it acts on a real Libby account). Every external call sits behind a
small ``_libby_*`` / ``_start_*`` function so tests can replace them. Logging is honest
(#238): an event is written after the engine answered, never before.
"""

import logging
import os
import threading
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models.book import Book
from ..models.external_import import ExternalImport
from ..models.inventory import Inventory
from ..models.reading import Reading
from ..models.wishlist_acquire import WishlistAcquireState
from ..routes.libby import LIBBY_ENGINE_URL, _core_tokens, _title_sim, _tokens
from . import libby_autofulfill_service as af
from .event_log_service import log_event

logger = logging.getLogger(__name__)

SETTING_ENABLED = "wishlist_acquire_enabled"
SETTING_INTERVAL = "wishlist_acquire_interval_min"

DEFAULT_INTERVAL_MIN = int(os.environ.get("WISHLIST_ACQUIRE_INTERVAL_MIN", "10"))

# Statuses (#297).
NEVER_TRIED = "never_tried"
ACQUIRED = "acquired"
ON_HOLD = "on_hold"
UNAVAILABLE = "unavailable"
AT_HOLD_LIMIT = "at_hold_limit"
AT_LOAN_LIMIT = "at_loan_limit"          # available, but every card is at its loan limit (#297)
FORMAT_BLOCKED = "format_blocked"
FAILED = "failed"
NO_MATCH = "no_match"
STATUSES = [NEVER_TRIED, ACQUIRED, ON_HOLD, UNAVAILABLE, AT_HOLD_LIMIT, AT_LOAN_LIMIT,
            FORMAT_BLOCKED, FAILED, NO_MATCH]

FORMATS = ["ebook", "audiobook"]          # ebook first (#297)

# Backoffs.
UNAVAILABLE_BACKOFF = timedelta(days=21)
NO_MATCH_BACKOFF = timedelta(days=30)
AT_LIMIT_BACKOFF = timedelta(days=1)
FORMAT_BLOCKED_BACKOFF = timedelta(days=3)
FAILED_BACKOFF_BASE = timedelta(hours=1)
FAILED_BACKOFF_CAP = timedelta(days=7)
BUSY_RETRY = timedelta(minutes=20)
# "acquired" is permanently terminal (#297): a book we ever borrowed is never borrowed again.
# "on_hold" re-opens after this long if it is still a Wishlist book and no longer on
# hold/loan on any card (checked at pick time).
ON_HOLD_REOPEN = timedelta(days=30)
MIN_AUTHOR_TOKEN = 3

# Title similarity needed (on top of MANDATORY author agreement) before we act on a
# Libby row. Stricter than the ownership matcher's 0.6 because this one spends loans.
TITLE_SIM_MIN = 0.8
AUTHOR_SIM_MIN = 0.34
# Don't borrow while this many loans are already waiting on #179's confirm/return tail.
MAX_PENDING = int(os.environ.get("WISHLIST_ACQUIRE_MAX_PENDING", "6"))

_ENGINE_TIMEOUT = 25.0
_SEARCH_TIMEOUT = 45.0

# (#297) One pass at a time, process-wide (scheduler job + manual route share it).
_RUN_LOCK = threading.Lock()


# ── settings ──────────────────────────────────────────────────────────────────

def is_enabled(db: Session) -> bool:
    return str(af._get(db, SETTING_ENABLED, "0")) in ("1", "true", "True")


def effective_interval(db: Session) -> int:
    return max(1, af._get_int(db, SETTING_INTERVAL, DEFAULT_INTERVAL_MIN))


# ── external Libby calls (the injectable boundary) ────────────────────────────
# Reads go straight to the engine (GET). The three that ACT on the real account are
# _borrow_ebook, _start_audiobook and _place_hold — tests replace all of them.

def _engine_get(path: str, params=None, timeout: float = _ENGINE_TIMEOUT) -> dict:
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{LIBBY_ENGINE_URL}{path}", params=params)
    r.raise_for_status()
    return r.json()


def _libby_cards() -> list:
    """Cards with usage, limits, ``can_harvest`` (prbn:v chip) and auth state."""
    return (_engine_get("/api/cards/status") or {}).get("cards", [])


def _libby_holds() -> list:
    return (_engine_get("/api/holds") or {}).get("holds", [])


def _libby_loans() -> list:
    return (_engine_get("/api/loans") or {}).get("loans", [])


def _libby_search(title: str, media: str) -> list:
    """Search every credentialed library; rows carry ``libraries`` [{cardId, titleId,
    isAvailable, holdsCount, key}]."""
    return (_engine_get("/api/search", {"q": title, "media": media, "per_page": 50},
                        timeout=_SEARCH_TIMEOUT) or {}).get("results", [])


def _borrow_ebook(title_id: str, card_id: str, title: str) -> tuple:
    """REAL borrow + .acsm download (no_return — the #179 tail returns it). Same engine
    call #179 uses. Returns (ok, detail)."""
    return af.kick_download(title_id, card_id, title)


def _audiobook_busy() -> bool:
    """The engine runs ONE audiobook job at a time (global status)."""
    try:
        return bool((_engine_get("/api/audiobook/download/status") or {}).get("active"))
    except Exception:
        return True      # unknown -> assume busy; never start a second job blind


def _start_audiobook(title_id: str, card_id: str) -> tuple:
    """REAL borrow + headless harvest kick-off (background job on the engine). Returns
    (accepted, detail). ``accepted`` only means the job started — files landing is
    confirmed later by the ABS import (#179 tail)."""
    status, data = af._engine_post(
        "/api/audiobook/download",
        {"title_id": title_id, "card_id": card_id, "borrow": True, "return_after": False},
        timeout=45.0,
    )
    if status >= 400:
        return False, (data or {}).get("error", f"HTTP {status}")
    if str((data or {}).get("title_id") or "") != str(title_id):
        return False, "engine is running a different audiobook job"
    return True, str((data or {}).get("message") or "started")


def _place_hold(title_id: str, card_id: str) -> tuple:
    """REAL hold. Returns (ok, detail)."""
    status, data = af._engine_post("/api/holds/place", {"title_id": title_id, "card_id": card_id})
    if status < 400 and (data or {}).get("success"):
        return True, ""
    return False, (data or {}).get("error", f"HTTP {status}")


# ── matcher ───────────────────────────────────────────────────────────────────

def _match_row(title: str, author: str, row: dict) -> bool:
    """Title AND author must agree. This is the #135/#172 ownership matcher's logic
    (``routes.libby._title_sim`` Jaccard, subtitle-tolerant #204, author-token overlap
    >= 0.34) with its escape hatches removed: no title-only pass at sim >= 0.9 and no
    pass when either author is blank. Title bar raised to 0.8 because a false positive
    here spends a real loan/hold."""
    # (#297) Initials ("J. K.") are noise: only tokens >= 3 chars count, and at least one
    # shared token (a surname) is required on top of the overlap bar.
    in_a = {t for t in _tokens(author) if len(t) >= MIN_AUTHOR_TOKEN}
    row_a = {t for t in _tokens(row.get("author") or row.get("firstCreatorName") or "")
             if len(t) >= MIN_AUTHOR_TOKEN}
    if not in_a or not row_a or not (in_a & row_a):
        return False
    in_t = _tokens(title)
    if not in_t:
        return False
    sim = _title_sim(in_t, _core_tokens(title), _tokens(row.get("title") or ""),
                     _core_tokens(row.get("title") or ""))
    if sim < TITLE_SIM_MIN:
        return False
    union = len(in_a | row_a)
    return (len(in_a & row_a) / union if union else 0.0) >= AUTHOR_SIM_MIN


# ── candidate selection ───────────────────────────────────────────────────────

def _reading_series(db: Session) -> set:
    """Lower-cased series names with an in-progress or finished reading."""
    today = date.today()
    rows = (
        db.query(func.lower(Book.series))
        .join(Reading, Reading.book_id == Book.id)
        .filter(Book.series.isnot(None), func.trim(Book.series) != "",
                Reading.date_started.isnot(None), Reading.date_started <= today,
                or_(Reading.date_finished_actual.isnot(None),                      # finished
                    (Reading.date_finished_actual.is_(None) & Reading.date_dnf.is_(None))))  # in progress
        .distinct().all()
    )
    return {r[0].strip() for r in rows if r[0]}


def _eligible_books(db: Session, fmt: str) -> list:
    """Wishlist books (#297: the project's Wishlist definition, as dedup_service /
    metadata_backfill_service): owned in NO format and no ExternalImport; non-blank
    title. ``fmt`` doesn't narrow it — once anything is owned/imported the book is
    done for every format. Order: books in a series being/been read first, then highest id."""
    owned_q = db.query(Inventory.book_id).filter(
        or_(Inventory.owned_physical == True, Inventory.owned_ebook == True,   # noqa: E712
            Inventory.owned_audio == True))
    ext_q = db.query(ExternalImport.book_id)
    books = (
        db.query(Book)
        .filter(Book.title.isnot(None), func.trim(Book.title) != "",
                ~Book.id.in_(owned_q), ~Book.id.in_(ext_q))
        .all()
    )
    series = _reading_series(db)

    def key(b):
        in_series = bool(b.series and b.series.strip().lower() in series)
        return (0 if in_series else 1, -b.id)

    return sorted(books, key=key)


def _due(state, now: datetime) -> bool:
    if state is None:
        return True
    if state.status == ACQUIRED:
        return False            # (#297) permanently terminal: never borrow it again
    if state.status == ON_HOLD and state.next_attempt_at is None:
        # Legacy row without a re-open time: re-open ON_HOLD_REOPEN after the attempt.
        return (state.last_attempt_at is not None
                and state.last_attempt_at + ON_HOLD_REOPEN <= now)
    return state.next_attempt_at is None or state.next_attempt_at <= now


def pick_candidate(db: Session, now: datetime | None = None):
    """First (book, format) that is eligible and outside its backoff, or None.
    All ebook candidates come before any audiobook candidate."""
    now = now or datetime.utcnow()
    states = {(s.book_id, s.format): s for s in db.query(WishlistAcquireState).all()}
    for fmt in FORMATS:
        for b in _eligible_books(db, fmt):
            if _due(states.get((b.id, fmt)), now):
                return b, fmt
    return None


# ── state recording ───────────────────────────────────────────────────────────

def _record(db: Session, book: Book, fmt: str, status: str, reason: str, *,
            card_id: str = "", library: str = "", title_id: str = "",
            next_at: datetime | None = None, count_attempt: bool = True) -> WishlistAcquireState:
    now = datetime.utcnow()
    st = (db.query(WishlistAcquireState)
          .filter_by(book_id=book.id, format=fmt).first())
    if st is None:
        st = WishlistAcquireState(book_id=book.id, format=fmt, attempts=0)
        db.add(st)
    if count_attempt:
        st.attempts = (st.attempts or 0) + 1
    st.status = status
    st.last_attempt_at = now
    st.last_reason = (reason or "")[:500]
    if card_id:
        st.card_id = card_id
    if library:
        st.library = library
    if title_id:
        st.libby_title_id = title_id
    if next_at is not None:
        st.next_attempt_at = next_at
    elif status == UNAVAILABLE:
        st.next_attempt_at = now + UNAVAILABLE_BACKOFF
    elif status == NO_MATCH:
        st.next_attempt_at = now + NO_MATCH_BACKOFF
    elif status in (AT_HOLD_LIMIT, AT_LOAN_LIMIT):
        st.next_attempt_at = now + AT_LIMIT_BACKOFF
    elif status == FORMAT_BLOCKED:
        st.next_attempt_at = now + FORMAT_BLOCKED_BACKOFF
    elif status == FAILED:
        n = max(1, st.attempts or 1)
        st.next_attempt_at = now + min(FAILED_BACKOFF_BASE * (2 ** (n - 1)), FAILED_BACKOFF_CAP)
    elif status == ACQUIRED:
        st.next_attempt_at = None           # terminal forever (#297)
    elif status == ON_HOLD:
        # #179 takes over; re-open after 30d (pick time re-checks hold/loan state, and
        # eligibility re-checks that it is still a Wishlist book).
        st.next_attempt_at = now + ON_HOLD_REOPEN
    else:
        st.next_attempt_at = None
    db.commit()
    return st


_LEVEL = {ACQUIRED: "success", ON_HOLD: "success", FAILED: "error"}


def _outcome(db: Session, book: Book, fmt: str, status: str, reason: str, *, level: str | None = None,
             **kw) -> dict:
    """Record the state, THEN log it (#238: never log before it happened)."""
    _record(db, book, fmt, status, reason, **kw)
    log_event("libby", "wishlist_acquire", level=level or _LEVEL.get(status, "info"),
              book_id=book.id, title=book.title,
              detail={"format": fmt, "status": status, "reason": reason,
                      "card_id": kw.get("card_id", ""), "library": kw.get("library", ""),
                      "title_id": kw.get("title_id", "")})
    return {"book_id": book.id, "title": book.title, "format": fmt, "status": status, "reason": reason}


# ── the attempt ───────────────────────────────────────────────────────────────

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _card_usage(cards: list, holds: list, loans: list) -> dict:
    """cardId -> {loans, holds, loan_limit, hold_limit, harvest, name, dead}. Counts are
    the larger of the engine's card counters and the actual holds/loans lists, because a
    capacity number must not be trusted unchecked (#297: loans once under-reported)."""
    out = {}
    for c in cards:
        cid = str(c.get("cardId", ""))
        n_loans = sum(1 for l in loans if str(l.get("cardId", "")) == cid)
        n_holds = sum(1 for h in holds if str(h.get("cardId", "")) == cid)
        out[cid] = {
            "loans": max(_int(c.get("loans")) or 0, n_loans),
            "holds": max(_int(c.get("holds")) or 0, n_holds),
            "loan_limit": _int(c.get("loanLimit")),
            "hold_limit": _int(c.get("holdLimit")),
            "harvest": bool(c.get("can_harvest")),
            "name": c.get("name") or c.get("cardLabel") or cid,
            "dead": bool(c.get("card_expired") or c.get("auth_error")),
        }
    return out


def _attempt(db: Session, book: Book, fmt: str) -> dict:
    title = (book.title or "").strip()
    author = book.author or ""
    if not author.strip():
        return _outcome(db, book, fmt, NO_MATCH,
                        "wishlist book has no author — refusing to match on title alone")

    # Engine state first: an outage must not burn the book's backoff.
    try:
        cards = _libby_cards()
        holds = _libby_holds()
        loans = _libby_loans()
    except Exception as exc:
        log_event("libby", "wishlist_acquire", level="warn", book_id=book.id, title=title,
                  detail={"format": fmt, "status": "engine_unreachable", "reason": str(exc)[:300]})
        return {"book_id": book.id, "title": title, "format": fmt, "error": f"engine: {exc}"}
    usage = _card_usage(cards, holds, loans)

    try:
        rows = _libby_search(title, fmt)
    except Exception as exc:
        return _outcome(db, book, fmt, FAILED, f"search failed: {exc}"[:300])

    if not rows:
        return _outcome(db, book, fmt, UNAVAILABLE, "not in any of our libraries' catalogues")
    matched_all = [r for r in rows if _match_row(title, author, r)]
    if not matched_all:
        return _outcome(db, book, fmt, NO_MATCH,
                        f"{len(rows)} result(s) but none agree on title AND author")
    # (#297) Rows the engine flags inLibrary are already ours (local-library check, same
    # flag routes/libby.py and #179 use): never act on them.
    matched = [r for r in matched_all if not r.get("inLibrary")]
    if not matched:
        return _outcome(db, book, fmt, NO_MATCH,
                        "matching result(s) are flagged inLibrary (already owned) — skipped")
    held = [r for r in matched if r.get("onHold")]
    if held:
        return _outcome(db, book, fmt, ON_HOLD, "already on hold (search flag onHold)",
                        title_id=str(held[0].get("id", "")), level="info")

    # One entry per (library, edition) across every matching row.
    libs = []
    for r in matched:
        for lib in (r.get("libraries") or []):
            cid = str(lib.get("cardId", ""))
            tid = str(lib.get("titleId") or r.get("id") or "")
            if cid and tid and cid in usage and not usage[cid]["dead"]:
                libs.append({**lib, "cardId": cid, "titleId": tid, "u": usage[cid]})
    if not libs:
        return _outcome(db, book, fmt, UNAVAILABLE,
                        "matched, but no usable (signed-in) card carries it")

    # Already ours? Don't double up on a title we hold or have on loan.
    tids = {l["titleId"] for l in libs}
    for h in holds:
        if str(h.get("id", "")) in tids:
            return _outcome(db, book, fmt, ON_HOLD, "already on hold",
                            card_id=str(h.get("cardId", "")), title_id=str(h.get("id", "")),
                            level="info")
    for l in loans:
        if str(l.get("id", "")) in tids:
            lt, lc = str(l.get("id", "")), str(l.get("cardId", ""))
            note = ""
            if fmt == "ebook":
                # (#297) A loan we don't track would leak a slot: hand it to #179 so it
                # confirms the import and returns it (idempotent per title_id).
                note = _enqueue_safe(db, book, lt, lc, title, author)
            return _outcome(db, book, fmt, ACQUIRED, f"already on loan (not borrowed again){note}",
                            card_id=lc, title_id=lt, level="info")

    if fmt == "audiobook":
        chip = [l for l in libs if l["u"]["harvest"]]
        if not chip:
            return _outcome(db, book, fmt, FORMAT_BLOCKED,
                            "audiobook only on cards not on the prbn:v chip (cannot be harvested)")
        libs = chip

    # (a) available + loan headroom -> borrow.
    avail = [l for l in libs if l.get("isAvailable")]
    with_room = [l for l in avail
                 if l["u"]["loan_limit"] is not None and l["u"]["loans"] < l["u"]["loan_limit"]]
    if with_room:
        pick = max(with_room, key=lambda l: l["u"]["loan_limit"] - l["u"]["loans"])
        return _borrow(db, book, fmt, pick)
    if avail:
        return _outcome(db, book, fmt, AT_LOAN_LIMIT,
                        "available, but every card carrying it is at its loan limit (or limit unknown)")

    # (b) not available -> hold.
    if fmt == "audiobook":
        # #179 only auto-claims EBOOK holds, so an audiobook hold would sit until it
        # expired. Record instead of parking a slot nothing will collect.
        return _outcome(db, book, fmt, UNAVAILABLE,
                        "not available now; audiobook holds are not auto-claimed by #179 (ebook-only)")
    roomy = []
    for l in libs:
        lim = l["u"]["hold_limit"]
        cap = (lim // 2) if lim is not None else 0       # automatic holds: at most half the limit
        if l["u"]["holds"] < cap:
            roomy.append((cap - l["u"]["holds"], l))
    if not roomy:
        return _outcome(db, book, fmt, AT_HOLD_LIMIT,
                        "every card carrying it is at half its hold limit (reserved for manual holds)")
    _, pick = max(roomy, key=lambda t: (t[0], -(_int(t[1].get("holdsCount")) or 0)))
    ok, detail = _place_hold(pick["titleId"], pick["cardId"])
    if not ok:
        return _outcome(db, book, fmt, FAILED, f"hold failed: {detail}",
                        card_id=pick["cardId"], library=pick["u"]["name"], title_id=pick["titleId"])
    return _outcome(db, book, fmt, ON_HOLD, "hold placed (auto-claimed by #179 when ready)",
                    card_id=pick["cardId"], library=pick["u"]["name"], title_id=pick["titleId"])


def _borrow(db: Session, book: Book, fmt: str, pick: dict) -> dict:
    """Borrow + download on ``pick`` and hand the loan to #179's confirm->return tail."""
    kw = dict(card_id=pick["cardId"], library=pick["u"]["name"], title_id=pick["titleId"])
    if len(af._get_json(db, af.SETTING_PENDING, [])) >= MAX_PENDING:
        return _outcome(db, book, fmt, NEVER_TRIED,
                        f"deferred: {MAX_PENDING} loans already awaiting import confirmation",
                        next_at=datetime.utcnow() + BUSY_RETRY, count_attempt=False,
                        level="info", **kw)
    title = book.title
    author = book.author or ""
    if fmt == "ebook":
        ok, detail = _borrow_ebook(pick["titleId"], pick["cardId"], title)
        if not ok:
            return _outcome(db, book, fmt, FAILED,
                            f"borrow/download failed: {detail} (a loan may be left checked out)", **kw)
        note = _enqueue_safe(db, book, pick["titleId"], pick["cardId"], title, author)
        return _outcome(db, book, fmt, ACQUIRED,
                        f"borrowed + .acsm downloaded ({detail or 'ok'}); awaiting import confirm, then returned{note}",
                        **kw)
    # audiobook
    if _audiobook_busy():
        return _outcome(db, book, fmt, NEVER_TRIED, "deferred: audiobook engine is busy with another job",
                        next_at=datetime.utcnow() + BUSY_RETRY, count_attempt=False, level="info", **kw)
    ok, detail = _start_audiobook(pick["titleId"], pick["cardId"])
    if not ok:
        return _outcome(db, book, fmt, FAILED, f"audiobook start failed: {detail}", **kw)
    note = _enqueue_safe(db, book, pick["titleId"], pick["cardId"], title, author)
    # Only the JOB START is known here — the harvest takes tens of minutes. Level info,
    # not success; "returned" happens only once the ABS import is confirmed.
    return _outcome(db, book, fmt, ACQUIRED,
                    f"audiobook borrow+harvest started ({detail}); loan returned only after ABS import is confirmed{note}",
                    level="info", **kw)


def _enqueue_safe(db: Session, book: Book, title_id: str, card_id: str, title: str,
                  author: str) -> str:
    """(#297) Hand a loan to #179's confirm->return tail. The borrow already HAPPENED, so
    a failure here must never turn the outcome into FAILED (it would be retried and
    double-borrow): warn and carry on. Returns a note for the reason ('' on success)."""
    try:
        af.enqueue_borrowed(db, title_id=title_id, card_id=card_id, title=title, author=author)
        return ""
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("wishlist_acquire: enqueue_borrowed failed for %s: %s", title, exc)
        log_event("libby", "wishlist_acquire", level="warn", book_id=book.id, title=title,
                  detail={"status": "enqueue_failed", "reason": str(exc)[:300],
                          "title_id": title_id, "card_id": card_id})
        return f" [WARNING: could not queue for confirm/return: {str(exc)[:120]}]"


def run_once(db: Session) -> dict:
    """One pass: pick ONE book/format and attempt it. No-op while the toggle is off.
    Serialised (#297): the scheduled job and the manual 'Run one now' share one lock, so
    two passes can never pick the same book and double-borrow."""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        return _run_once_locked(db)
    finally:
        _RUN_LOCK.release()


def _run_once_locked(db: Session) -> dict:
    if not is_enabled(db):
        return {"skipped": "disabled"}
    if not af.is_enabled(db):
        # The confirm->return tail and hold claiming live in #179's run; without it a
        # borrowed loan would never be returned and a hold never claimed.
        return {"skipped": "libby auto-fulfill (#179) is off — its confirm/return tail would not run"}
    cand = pick_candidate(db)
    if cand is None:
        return {"picked": None, "reason": "nothing eligible"}
    book, fmt = cand
    try:
        res = _attempt(db, book, fmt)
    except Exception as exc:                                 # never re-pick a poison book
        logger.exception("wishlist_acquire: %s/%s crashed", book.id, fmt)
        db.rollback()
        res = _outcome(db, book, fmt, FAILED, f"unexpected error: {type(exc).__name__}: {exc}"[:300])
    res["at"] = datetime.utcnow().isoformat()
    return res


# ── Settings card data ────────────────────────────────────────────────────────

def get_config(db: Session) -> dict:
    counts = {s: 0 for s in STATUSES}
    for status, n in db.query(WishlistAcquireState.status, func.count()).group_by(WishlistAcquireState.status):
        counts[status] = n
    recent = (
        db.query(WishlistAcquireState, Book.title)
        .join(Book, Book.id == WishlistAcquireState.book_id)
        .filter(WishlistAcquireState.last_attempt_at.isnot(None))
        .order_by(WishlistAcquireState.last_attempt_at.desc())
        .limit(10).all()
    )
    return {
        "enabled": is_enabled(db),
        "interval_min": effective_interval(db),
        "autofulfill_enabled": af.is_enabled(db),
        "counts": counts,
        "recent": [{**s.to_dict(), "title": t} for s, t in recent],
    }
