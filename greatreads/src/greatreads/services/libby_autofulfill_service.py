"""Auto-fulfill ready Libby holds (#179).

When a Libby HOLD becomes available, run the full acquisition loop — borrow →
download the .acsm (engine handles the OverDrive/Playwright path) → let the
acsm-watcher import it into Calibre/GreatReads → **confirm the import** → and
only THEN return the loan to free the slot. If the import never confirms, the
loan is left in place so the book is never lost.

Opt-in + default OFF (auto-borrowing/returning is aggressive). Cadence + per-run
cap are UI-configurable (mirrors the #159/#166 backfill pattern).

Design — stateful & non-blocking. The .acsm → Adobe → Calibre → watcher → sync
chain takes minutes, so we do NOT block a scheduler job waiting for it. Instead we
keep a small **pending-returns queue** (JSON in user_settings, so no DB migration)
and each run does two phases:

  1. Process pending: for every borrowed-but-not-returned title, check whether it
     has since imported (a fresh ExternalImport row matched to the hold by
     title+author via the #135 matcher). Imported → return the loan + drop it.
     Still missing past CONFIRM_TIMEOUT → drop it and LEAVE the loan (never return
     something we couldn't confirm).
  2. Acquire: pick isAvailable ebook holds that we don't already own / haven't
     already queued, borrow+download them with the engine's no_return flag (#179),
     and enqueue the successes — up to a per-run cap.

The engine's /api/download normally borrows→downloads→returns in one shot; we pass
``no_return=true`` so it stops after the download and hands return-timing to us.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..models.book import Book
from ..models.external_import import ExternalImport
from ..routes.libby import LIBBY_ENGINE_URL, _core_tokens, _match_owned, _tokens
from .event_log_service import log_event

logger = logging.getLogger(__name__)

# Setting keys (persisted in user_settings).
SETTING_ENABLED = "libby_autofulfill_enabled"
SETTING_INTERVAL = "libby_autofulfill_interval_min"
SETTING_MAX = "libby_autofulfill_max_per_run"
SETTING_PENDING = "libby_autofulfill_pending"
SETTING_LAST_RUN = "libby_autofulfill_last_run"
SETTING_FAILS = "libby_autofulfill_failures"

DEFAULT_INTERVAL_MIN = int(os.environ.get("LIBBY_AUTOFULFILL_INTERVAL_MIN", "30"))
DEFAULT_MAX_PER_RUN = int(os.environ.get("LIBBY_AUTOFULFILL_MAX_PER_RUN", "2"))
# How long to keep trying to confirm an import before giving up (and leaving the
# loan in place). The chain is usually minutes; allow generous slack for a stuck
# Adobe fulfillment / watcher.
CONFIRM_TIMEOUT_HOURS = float(os.environ.get("LIBBY_AUTOFULFILL_CONFIRM_TIMEOUT_H", "12"))
# Back-off (#201): after this many consecutive borrow/download failures for the
# same title, park it for the cooldown instead of retrying every run forever
# (Dark Matter failed identically 12+ times in one evening).
MAX_CONSECUTIVE_FAILS = int(os.environ.get("LIBBY_AUTOFULFILL_MAX_FAILS", "3"))
FAIL_COOLDOWN_HOURS = float(os.environ.get("LIBBY_AUTOFULFILL_FAIL_COOLDOWN_H", "24"))

_ENGINE_TIMEOUT = 25.0
_DOWNLOAD_TIMEOUT = 180.0


# ── user_settings helpers ─────────────────────────────────────────────────────

def _get(db: Session, key: str, default=None):
    from ..models.user_settings import UserSettings
    s = db.query(UserSettings).filter(UserSettings.setting_key == key).first()
    return s.setting_value if (s and s.setting_value is not None) else default


def _set(db: Session, key: str, value) -> None:
    from ..models.user_settings import UserSettings
    s = db.query(UserSettings).filter(UserSettings.setting_key == key).first()
    if s:
        s.setting_value = str(value)
    else:
        db.add(UserSettings(setting_key=key, setting_value=str(value)))
    db.commit()


def _get_int(db: Session, key: str, default: int) -> int:
    try:
        return int(_get(db, key, default))
    except (ValueError, TypeError):
        return default


def _get_json(db: Session, key: str, default):
    raw = _get(db, key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def is_enabled(db: Session) -> bool:
    return str(_get(db, SETTING_ENABLED, "0")) in ("1", "true", "True")


def effective_interval(db: Session) -> int:
    return _get_int(db, SETTING_INTERVAL, DEFAULT_INTERVAL_MIN)


def effective_max(db: Session) -> int:
    return max(1, _get_int(db, SETTING_MAX, DEFAULT_MAX_PER_RUN))


def get_config(db: Session) -> dict:
    """Config + live state for the Settings card."""
    pending = _get_json(db, SETTING_PENDING, [])
    return {
        "enabled": is_enabled(db),
        "interval_min": effective_interval(db),
        "max_per_run": effective_max(db),
        "confirm_timeout_hours": CONFIRM_TIMEOUT_HOURS,
        "pending": pending,
        "pending_count": len(pending),
        "last_run": _get_json(db, SETTING_LAST_RUN, None),
        "parked": {
            tid: f for tid, f in _get_json(db, SETTING_FAILS, {}).items()
            if int(f.get("count", 0)) >= MAX_CONSECUTIVE_FAILS
        },
    }


# ── engine calls (sync — this runs in a scheduler thread) ─────────────────────

def _engine_get(path: str, timeout: float = _ENGINE_TIMEOUT) -> dict:
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{LIBBY_ENGINE_URL}{path}")
    r.raise_for_status()
    return r.json()


def _engine_post(path: str, body: dict, timeout: float = _ENGINE_TIMEOUT):
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{LIBBY_ENGINE_URL}{path}", json=body)
    try:
        data = r.json()
    except Exception:
        data = {}
    return r.status_code, data


# ── import confirmation ───────────────────────────────────────────────────────

def _imported_since(db: Session, title: str, author: str, since: datetime) -> bool:
    """True if a book matching (title, author) has an ExternalImport at/after
    ``since`` — i.e. the borrowed .acsm has landed in Calibre and synced in."""
    rows = (
        db.query(ExternalImport.book_id)
        .filter(ExternalImport.imported_at >= since)
        .all()
    )
    ids = {r[0] for r in rows}
    if not ids:
        return False
    index = [
        {"book_id": b.id, "tt": _tokens(b.title), "core": _core_tokens(b.title), "at": _tokens(b.author or "")}
        for b in db.query(Book).filter(Book.id.in_(ids)).all()
    ]
    return _match_owned(title or "", author or "", index) is not None


# ── the run ───────────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.utcnow()


def _process_pending(db: Session, log: list) -> list:
    """Confirm-then-return each queued borrow. Returns the still-pending list."""
    pending = _get_json(db, SETTING_PENDING, [])
    if not pending:
        return []
    now = datetime.utcnow()
    keep = []
    for item in pending:
        title = item.get("title", "")
        author = item.get("author", "")
        borrowed_at = _parse_dt(item.get("borrowed_at", ""))
        # Confirm against imports since (just before) the borrow.
        since = borrowed_at - timedelta(minutes=2)
        if _imported_since(db, title, author, since):
            status, data = _engine_post(
                "/api/loans/return",
                {"title_id": item.get("title_id", ""), "card_id": item.get("card_id", "")},
            )
            if status < 400:
                log.append({"title": title, "event": "returned", "detail": "import confirmed"})
                log_event("libby", "return", level="success", title=title,
                          detail={"reason": "import confirmed"})
            else:
                # Import is confirmed but the return failed — keep it so we retry
                # the return next run (the loan is a spare copy, not lost).
                item["return_error"] = (data or {}).get("error", f"HTTP {status}")
                keep.append(item)
                log.append({"title": title, "event": "return_failed", "detail": item["return_error"]})
            continue
        # Not yet imported.
        if now - borrowed_at > timedelta(hours=CONFIRM_TIMEOUT_HOURS):
            log.append({"title": title, "event": "gave_up",
                        "detail": f"no import after {CONFIRM_TIMEOUT_HOURS:g}h — loan left in place"})
            log_event("libby", "autofulfill_giveup", level="warn", title=title,
                      detail={"after_hours": CONFIRM_TIMEOUT_HOURS})
            # Drop it: stop retrying, but never auto-return an unconfirmed loan.
            continue
        keep.append(item)
    return keep


def _acquire(db: Session, pending: list, max_new: int, log: list) -> list:
    """Borrow+download ready ebook holds we don't own / haven't queued. Appends
    successes to ``pending`` and returns it."""
    if max_new <= 0:
        return pending
    try:
        holds = (_engine_get("/api/holds") or {}).get("holds", [])
    except Exception as exc:
        log.append({"event": "holds_error", "detail": str(exc)})
        return pending

    queued_ids = {str(p.get("title_id")) for p in pending}
    fails = _get_json(db, SETTING_FAILS, {})
    hold_ids = {str(h.get("id", "")) for h in holds}
    # Drop failure history for holds that no longer exist (cancelled/claimed).
    fails = {tid: f for tid, f in fails.items() if tid in hold_ids}
    now = datetime.utcnow()
    taken = 0
    for h in holds:
        if taken >= max_new:
            break
        if not h.get("isAvailable"):
            continue
        # Only ebooks can go through the .acsm import path.
        if (h.get("holdType") or "ebook") != "ebook":
            continue
        # Already owned (engine's local-library check) → skip; nothing to acquire.
        if h.get("inLibrary"):
            continue
        title_id = str(h.get("id", ""))
        card_id = str(h.get("cardId", ""))
        if not title_id or not card_id or title_id in queued_ids:
            continue
        # Parked after repeated identical failures (#201) — retry once per cooldown.
        prior = fails.get(title_id)
        if prior and int(prior.get("count", 0)) >= MAX_CONSECUTIVE_FAILS:
            if now - _parse_dt(prior.get("last_at", "")) < timedelta(hours=FAIL_COOLDOWN_HOURS):
                continue
        title = h.get("title", "")
        author = h.get("author", "")
        try:
            status, data = _engine_post(
                "/api/download",
                {"title_id": title_id, "card_id": card_id, "title": title, "no_return": True},
                timeout=_DOWNLOAD_TIMEOUT,
            )
        except Exception as exc:
            status, data = 599, {"error": str(exc)}
        if status < 400 and (data or {}).get("success"):
            fails.pop(title_id, None)
            item = {
                "title_id": title_id, "card_id": card_id,
                "title": title, "author": author,
                "borrowed_at": datetime.utcnow().isoformat(),
            }
            pending.append(item)
            queued_ids.add(title_id)
            taken += 1
            fname = (data or {}).get("filename", "downloaded")
            log.append({"title": title, "event": "borrowed", "detail": fname})
            log_event("libby", "auto_borrow", level="success", title=title,
                      detail={"author": author, "file": fname})
            log_event("libby", "acsm_download", level="info", title=title, detail={"file": fname})
        else:
            err = (data or {}).get("error", f"HTTP {status}")
            entry = fails.get(title_id) or {"count": 0}
            entry.update({
                "count": int(entry.get("count", 0)) + 1,
                "last_at": now.isoformat(),
                "error": err,
                "title": title,
            })
            fails[title_id] = entry
            log.append({"title": title, "event": "download_failed", "detail": err})
            if entry["count"] == MAX_CONSECUTIVE_FAILS:
                log_event("libby", "autofulfill_parked", level="warn", title=title,
                          detail={"error": err, "fails": entry["count"],
                                  "cooldown_hours": FAIL_COOLDOWN_HOURS,
                                  "card_id": card_id})
                log.append({"title": title, "event": "parked",
                            "detail": f"{entry['count']} straight failures — retrying every {FAIL_COOLDOWN_HOURS:g}h"})
            else:
                log_event("libby", "borrow_failed", level="error", title=title, detail={"error": err})
    _set(db, SETTING_FAILS, json.dumps(fails))
    return pending


def run_autofulfill(db: Session, *, force: bool = False) -> dict:
    """One auto-fulfill pass. ``force`` runs even when the toggle is off (manual
    'Run now'). Records a last-run summary for the Settings card."""
    if not force and not is_enabled(db):
        return {"skipped": "disabled"}

    log: list = []
    pending = _process_pending(db, log)
    # Don't let the in-flight queue grow unbounded: cap new borrows so total
    # pending never exceeds ~2× the per-run cap.
    cap = effective_max(db)
    room = max(0, (2 * cap) - len(pending))
    pending = _acquire(db, pending, min(cap, room), log)

    _set(db, SETTING_PENDING, json.dumps(pending))
    summary = {
        "at": datetime.utcnow().isoformat(),
        "borrowed": sum(1 for e in log if e.get("event") == "borrowed"),
        "returned": sum(1 for e in log if e.get("event") == "returned"),
        "pending": len(pending),
        "events": log[-20:],
    }
    _set(db, SETTING_LAST_RUN, json.dumps(summary))
    if log:
        logger.info("Libby auto-fulfill: %s", {k: v for k, v in summary.items() if k != "events"})
    return summary


# ── UI-borrow takeover (#203) ─────────────────────────────────────────────────

def enqueue_borrowed(db: Session, *, title_id: str, card_id: str, title: str, author: str = "") -> bool:
    """Hand an already-borrowed loan to the confirm→return pipeline.

    Used by the UI Borrow button (#203): the user (or the borrow route) claimed
    the hold; from here the normal machinery downloads the .acsm, waits for the
    watcher import, and only then returns the loan. Returns False if the title
    is already queued."""
    pending = _get_json(db, SETTING_PENDING, [])
    if any(str(p.get("title_id")) == str(title_id) for p in pending):
        return False
    pending.append({
        "title_id": str(title_id), "card_id": str(card_id),
        "title": title, "author": author,
        "borrowed_at": datetime.utcnow().isoformat(),
    })
    _set(db, SETTING_PENDING, json.dumps(pending))
    return True


def kick_download(title_id: str, card_id: str, title: str = "") -> tuple[bool, str]:
    """Fire the engine download for an EXISTING loan (no_return — loan is kept;
    the pipeline returns it after the import confirms). Returns (ok, detail)."""
    try:
        status, data = _engine_post(
            "/api/download",
            {"title_id": str(title_id), "card_id": str(card_id), "title": title, "no_return": True},
            timeout=_DOWNLOAD_TIMEOUT,
        )
    except Exception as exc:
        return False, str(exc)
    if status < 400 and (data or {}).get("success"):
        return True, (data or {}).get("filename", "")
    return False, (data or {}).get("error", f"HTTP {status}")
