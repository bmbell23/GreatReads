"""Wishlist de-dup + blank-row cleanup (#216).

Wishlist = a Book with no owned Inventory copy and no ExternalImport (the #185 "library"
definition, same as news_service / metadata_backfill_service). Some Wishlist rows are
duplicates of books you already own ('Mindset' vs 'Mindset: The New Psychology of
Success'); this finds them with the importer's own matcher so Settings can offer the
#131 merge compare. Also finds/purges the blank-title junk rows."""
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import settings
from ..models.book import Book
from ..models.book_contributor import BookContributor
from ..models.external_import import ExternalImport
from ..models.inventory import Inventory
from ..models.reading import Reading
from .import_service import (_best_match, _build_existing, _title_core_tokens, _word_tokens,
                             get_calibre_authors_map, is_blank_author, title_similarity)

# 'Likely duplicate' bar from _best_match's docstring. Subtitle cases ('Mindset' vs
# 'Mindset: The New Psychology of Success') clear it via the #204 core-token path. (#216)
DUP_THRESHOLD = 0.85
# (#357) When one side's author is blank/'Unknown' the author can't corroborate, so a pair is
# listed on the title alone -- but only at this (subtitle-tolerant) title similarity, and it is
# flagged `author_unverified` so the review UI shows a warning.
UNVERIFIED_TITLE_SIM = 0.95

# "Not a duplicate" dismissals (#346): user_settings[dup_dismissed] = JSON list of
# "min_id:max_id" strings. find_duplicates() drops any pair listed here.
DISMISS_KEY = "dup_dismissed"

# In-process cache of the duplicate count for the nav badge (#346). find_duplicates is heavy
# (matcher + a read-only Calibre read), so the badge endpoint reads this instead; it is
# invalidated on merge / dismiss / purge and otherwise expires after COUNT_TTL seconds.
COUNT_TTL = 600
_count_cache: dict = {"n": None, "t": 0.0}


def pair_key(a: int, b: int) -> str:
    """Order-independent id for a pair: 'min:max' (#346)."""
    a, b = int(a), int(b)
    return f"{min(a, b)}:{max(a, b)}"


def get_dismissed(db: Session) -> set[str]:
    """The set of dismissed 'min:max' pair keys (empty if unset/corrupt) (#346)."""
    from ..models.user_settings import UserSettings
    s = db.query(UserSettings).filter(UserSettings.setting_key == DISMISS_KEY).first()
    if s and s.setting_value:
        try:
            return {str(x) for x in json.loads(s.setting_value)}
        except (ValueError, TypeError):
            pass
    return set()


def dismiss_pair(db: Session, a: int, b: int) -> str:
    """Persist 'not a duplicate' for the pair and drop the cached count (#346)."""
    from ..models.user_settings import UserSettings
    key = pair_key(a, b)
    keys = get_dismissed(db) | {key}
    value = json.dumps(sorted(keys))
    s = db.query(UserSettings).filter(UserSettings.setting_key == DISMISS_KEY).first()
    if s:
        s.setting_value = value
    else:
        db.add(UserSettings(setting_key=DISMISS_KEY, setting_value=value))
    db.commit()
    invalidate_count_cache()
    return key


def invalidate_count_cache() -> None:
    _count_cache["n"], _count_cache["t"] = None, 0.0


def set_count_cache(n: int) -> None:
    """Prime the cache from a fresh find_duplicates() result (#346)."""
    _count_cache["n"], _count_cache["t"] = int(n), time.monotonic()


def duplicate_count(db: Session) -> int:
    """Cached count of reviewable pairs; computed (and cached) when cold/expired (#346).
    This computes inline (~10s) -- request handlers must use cached_count() instead (#363)."""
    n = _count_cache["n"]
    if n is not None and time.monotonic() - _count_cache["t"] < COUNT_TTL:
        return n
    n = len(find_duplicates(db))
    set_count_cache(n)
    return n


_refresh_lock = threading.Lock()
_refreshing = False


def _refresh_worker() -> None:
    global _refreshing
    from ..database import SessionLocal
    db = SessionLocal()   # fresh session owned by this thread (#363)
    try:
        set_count_cache(len(find_duplicates(db)))
    except Exception:
        logging.getLogger(__name__).exception("duplicate count refresh failed")
    finally:
        db.close()
        with _refresh_lock:
            _refreshing = False


def cached_count() -> dict:
    """Never-blocking count for page loads (#363): returns the cached number (possibly stale,
    or None when cold) and starts a single background refresh if it is cold/expired.
    {"count": int|None, "computing": bool}"""
    global _refreshing
    n = _count_cache["n"]
    fresh = n is not None and time.monotonic() - _count_cache["t"] < COUNT_TTL
    if not fresh:
        with _refresh_lock:
            if not _refreshing:
                _refreshing = True
                threading.Thread(target=_refresh_worker, name="dup-count-refresh",
                                 daemon=True).start()
    return {"count": n, "computing": not fresh}


def _owned_ids(db: Session) -> set[int]:
    """Book ids that are in the library: an owned inventory copy OR an import (#185)."""
    inv = db.query(Inventory.book_id).filter(
        or_(Inventory.owned_ebook == True, Inventory.owned_audio == True,  # noqa: E712
            Inventory.owned_physical == True))
    ext = db.query(ExternalImport.book_id)
    return {r[0] for r in inv.all()} | {r[0] for r in ext.all()}


def _author(b) -> str:
    return " ".join(filter(None, [b.author_name_first, b.author_name_second]))


def _match_either(title, author, series, num, year, stored, full):
    """Best of matching on stored authors vs Calibre full author lists (never worse
    than the pre-#344 behaviour); stored wins ties."""
    a = _best_match(title, author[0], series, num, year, stored)
    b = _best_match(title, author[1], series, num, year, full)
    return b if (b and (not a or b["score"] > a["score"])) else a


def _other_entry(b, m) -> bool:
    """Different numbered entries of one series are different books, however alike
    the titles: 'Thrawn' (#1) vs 'Thrawn: Alliances' (#2) (#344). Only when BOTH
    have a series: Calibre stamps series_index 1.0 on every unseriesed book."""
    if not ((b.series or "").strip() and (m.get("series") or "").strip()):
        return False
    a, c = b.series_number, m.get("series_number")
    return a is not None and c is not None and float(a) != float(c)


def _unverified_match(b, my_author: str, candidates: list, by_id: dict):
    """(#357) Best owned candidate whose title matches ``b`` at >= UNVERIFIED_TITLE_SIM
    (subtitle tolerance) where ``b``'s author or the candidate's is blank/'Unknown'. Only
    consulted when the normal matcher found nothing: an 'Unknown' author drags the composite
    score under the bar. Returns (id, sim) or None."""
    tok, core = _word_tokens(b.title), _title_core_tokens(b.title)
    if not tok:
        return None
    best = None
    for e in candidates:
        if e[0] == b.id or not e[3]:
            continue
        if not (is_blank_author(my_author) or is_blank_author(by_id[e[0]][2])):
            continue
        sim = title_similarity(tok, core, e[1], e[3])
        if sim >= UNVERIFIED_TITLE_SIM and (best is None or sim > best[1]
                                            or (sim == best[1] and e[0] < best[0])):
            best = (e[0], sim)
    return best


def find_duplicates(db: Session, threshold: float = DUP_THRESHOLD) -> list[dict]:
    """Likely duplicates at >= threshold, best score first. Two kinds (#344):
      "wishlist" — a Wishlist book matching an OWNED book (`wishlist` = loser, `owned` = survivor)
      "owned"    — two owned books that are the same work (each unordered pair once;
                   `survivor` = the lower/older id, `loser` = the other)
    Every pair also carries `survivor`/`loser` {id,title,author} for the merge link, and
    `author_unverified` (#357): True when either side's author is blank/'Unknown', so the
    author could not corroborate the title match."""
    owned = _owned_ids(db)
    everything = _build_existing(db)
    existing = [e for e in everything if e[0] in owned]   # candidates: owned only
    by_id = {e[0]: e for e in existing}                   # display titles/authors (stored)
    # #344: GR keeps ONE author per book, so two records of one book imported with
    # different arbitrary co-authors never agree. Match the OWNED side on Calibre's
    # full author list where we have it (stored author is still what's displayed).
    full_auth = get_calibre_authors_map(db)
    stored = existing
    existing = [
        (e[0], e[1], full_auth[e[0]], e[3], e[4], e[5], e[6], _word_tokens(full_auth[e[0]]), e[8])
        if e[0] in full_auth else e
        for e in existing
    ]
    pairs = []
    seen_owned: set[tuple[int, int]] = set()
    for b in db.query(Book).order_by(Book.id).all():
        if not (b.title or "").strip():
            continue
        author = (_author(b), full_auth.get(b.id) or _author(b))   # (stored, full)
        year = str(b.date_published.year) if b.date_published else None
        if b.id in owned:
            # Owned ↔ owned: match against the OTHER owned books (never itself).
            m = _match_either(b.title, author, b.series, b.series_number, year,
                              [e for e in stored if e[0] != b.id],
                              [e for e in existing if e[0] != b.id])
            if not m or m["id"] not in by_id or m["score"] < threshold or _other_entry(b, m):
                m = None
                u = _unverified_match(b, author[0], stored, by_id)      # (#357)
                if u:
                    m = {"id": u[0], "score": round(u[1], 2)}
                    for e in stored:
                        if e[0] == u[0]:
                            m.update(title=e[1], series=e[4], series_number=e[5])
                    if _other_entry(b, m):
                        m = None
                if not m:
                    continue
            key = (min(b.id, m["id"]), max(b.id, m["id"]))
            if key in seen_owned:
                continue
            seen_owned.add(key)
            lo, hi = key
            side = lambda i: {"id": i, "title": by_id[i][1], "author": by_id[i][2]}  # noqa: E731
            pairs.append({
                "kind": "owned",
                "survivor": side(lo), "loser": side(hi),
                "score": m["score"],
                "author_unverified": is_blank_author(by_id[lo][2]) or is_blank_author(by_id[hi][2]),
            })
            continue
        m = _match_either(b.title, author, b.series, b.series_number, year, stored, existing)
        if not m or m["id"] == b.id or m["id"] not in by_id or m["score"] < threshold or _other_entry(b, m):
            m = None
            u = _unverified_match(b, author[0], stored, by_id)          # (#357)
            if u:
                m = {"id": u[0], "score": round(u[1], 2)}
                for e in stored:
                    if e[0] == u[0]:
                        m.update(title=e[1], series=e[4], series_number=e[5])
                if _other_entry(b, m):
                    m = None
            if not m:
                continue
        owned_side = {"id": m["id"], "title": m["title"], "author": by_id[m["id"]][2]}
        wish_side = {"id": b.id, "title": b.title, "author": author[0]}
        pairs.append({
            "kind": "wishlist",
            "wishlist": wish_side,
            "owned": owned_side,
            "survivor": owned_side, "loser": wish_side,
            "score": m["score"],
            "author_unverified": is_blank_author(owned_side["author"]) or is_blank_author(wish_side["author"]),
        })
    dismissed = get_dismissed(db)   # #346: "Not a duplicate" pairs never come back
    if dismissed:
        pairs = [p for p in pairs if pair_key(p["survivor"]["id"], p["loser"]["id"]) not in dismissed]
    pairs.sort(key=lambda p: (-p["score"], p["survivor"]["id"], p["loser"]["id"]))
    return pairs


def find_blank(db: Session) -> list[dict]:
    """Rows with a NULL/whitespace title; `deletable` = not owned, no import, no readings."""
    owned = _owned_ids(db)
    imported = {r[0] for r in db.query(ExternalImport.book_id).all()}
    out = []
    for b in db.query(Book).order_by(Book.id).all():
        if (b.title or "").strip():
            continue
        n_read = db.query(Reading).filter(Reading.book_id == b.id).count()
        is_owned = b.id in owned
        has_imp = b.id in imported
        out.append({
            "id": b.id, "author": _author(b),
            "date_published": b.date_published.isoformat() if b.date_published else None,
            "owned": is_owned, "has_import": has_imp, "readings": n_read,
            "deletable": not is_owned and not has_imp and n_read == 0,
        })
    return out


def backup_db(db_path: str, backup_dir: Path) -> str:
    """Online-backup the SQLite file (safe on a live DB). Raises on any failure."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst_path = backup_dir / f"greatreads-pre-purge-blank-{ts}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return str(dst_path)


def purge_blank(db: Session, db_path: str, backup_dir: Optional[Path] = None) -> dict:
    """Back up the DB, then delete ONLY the deletable blank rows (recomputed here).
    Backup failure raises before anything is touched."""
    ids = [r["id"] for r in find_blank(db) if r["deletable"]]
    backup = backup_db(db_path, backup_dir or Path(db_path).parent / "backups")
    for bid in ids:
        book = db.query(Book).filter(Book.id == bid).first()
        if not book:
            continue
        # book_contributors has no cascade — delete them or they orphan (#290).
        db.query(BookContributor).filter(BookContributor.book_id == bid).delete(
            synchronize_session=False)
        book.tags = []   # clears the book_tags m2m rows
        db.delete(book)  # cascades readings + inventory
    db.commit()
    invalidate_count_cache()   # #346
    for bid in ids:   # tidy cover files like DELETE /{book_id} (#95)
        for p in (settings.covers_dir / f"{bid}.jpg",
                  Path("/app/data/covers_thumb") / f"{bid}.jpg"):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return {"deleted": ids, "backup": backup}
