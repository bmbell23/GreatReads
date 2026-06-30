"""News service — detect new/upcoming book releases for watched authors (#68 Phase A).

`poll_releases(db)` runs on a daily cron: it builds the watched-author set, queries
the Google Books API per author, runs every candidate through an ordered, individually
**logged** filter pipeline (the hard part — `inauthor:` returns box sets, foreign
editions, study-guide parasites, anthologies and reissues), collapses editions to one
row per work, and upserts survivors into `news_items`.

Filter pipeline (cheapest/highest-yield first):
  1. language gate (en)                       5. window (upcoming ~18mo / new ~4mo)
  2. author-exactness (primary author)        6. reissue guard (work's earliest edition)
  3. junk-type blocklist                      7. confidence gate (cover + ISBN-13)
  4. edition collapse (work-level dedupe)     8. already-owned (import_service._best_match)
                                              9. user-dismissal backstop (work_key)

Read-only against Calibre/ABS; only writes the GR `news_items` table.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..config import settings
from ..discovery.google_books_client import GoogleBooksClient
from ..models.book import Book
from ..models.inventory import Inventory
from ..models.news_item import NewsItem
from ..models.reading import Reading
from ..models.user_settings import UserSettings
from ..services.import_service import _best_match, _build_existing, _split_author, _word_tokens

logger = logging.getLogger(__name__)

# Tuning knobs (kept together so they're easy to adjust from the drop log).
NEW_WINDOW_DAYS = 365        # a release within the last year (and not owned) counts as "new"
REPRINT_MIN_AGE_YEARS = 2    # if a work was first published this many years before this edition → reprint
UPCOMING_MAX_DAYS = 550      # ignore "upcoming" placeholders further out than ~18 months
# Google's orderBy=newest is unreliable (upcoming titles land deep in the list), so we
# page several deep and date-filter ourselves — else releases past position ~40 are missed.
MAX_RESULTS_PER_AUTHOR = 120

EXCLUDED_KEY = "news_excluded_authors"
EXTRA_KEY = "news_extra_authors"

# Stage 3: junk-type title/subtitle blocklist. Deliberately conservative — the
# author-exactness gate already removes most parasite products (wrong "author").
_JUNK_RE = re.compile(
    r"\bbox(?:ed)?\s*set\b|\bboxset\b|\bomnibus\b|\bthe\s+complete\b"
    r"|\bcomplete\s+(?:series|collection|saga)\b"
    r"|\bbooks?\s*\d+\s*[-–—]\s*\d+\b|\b\d+\s*[-–—]\s*book\b"
    r"|\bsummary\s+of\b|^\s*summary\s*[:\-]|\bstudy\s+guide\b|\ba\s+guide\s+to\b"
    r"|\banalysis\s+of\b|\bworkbook\b|\bconversation\s+starters\b"
    r"|\b(?:coloring|colouring)\s+book\b|\bplanner\b|\bcalendar\b"
    # multi-book bundles/sets/collections (e.g. "4 Books Collection Set", "3 Ebook Collection", "- 2 Books")
    r"|\b\d+\s+(?:e-?)?books?\b|\bcollection\s+set\b|\b(?:e-?)?book\s+collection\b"
    # marketing-spam titles on repackaged/reissue listings (real titles don't shout):
    r"|\bpre-?order\b|\bmust-?read\b|\bbooktok\b|\bbestsell|\btaking\b.*\bby\s+storm\b"
    r"|\bnow\s+a\s+(?:major\s+)?(?:netflix|hbo|tv|movie|major\s+motion)\b",
    re.IGNORECASE,
)

# Comics / graphic novels — categorized into their own section (not dropped). Google's
# `categories` is unreliable (the Witcher comic returns just "Fiction"), so match titles too.
_COMIC_RE = re.compile(
    r"\bgraphic\s+novel\b|\bmanga\b|\bcomics?\b|\bvol\.?\s*\d+\b"
    r"|\bthe\s+official\b.*\b(?:comic|graphic|illustrated)\b",
    re.IGNORECASE,
)


# ── settings-backed watch list helpers ──────────────────────────────────────
def _get_list_setting(db: Session, key: str) -> list[str]:
    s = db.query(UserSettings).filter(UserSettings.setting_key == key).first()
    if not s:
        return []
    try:
        val = json.loads(s.setting_value)
        return [str(x) for x in val] if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _set_list_setting(db: Session, key: str, values: list[str]) -> None:
    # de-dupe case-insensitively, preserve first-seen casing, keep sorted
    seen, out = set(), []
    for v in values:
        v = (v or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    out.sort(key=str.lower)
    s = db.query(UserSettings).filter(UserSettings.setting_key == key).first()
    if s:
        s.setting_value = json.dumps(out)
    else:
        db.add(UserSettings(setting_key=key, setting_value=json.dumps(out)))
    db.commit()


def _read_authors(db: Session) -> list[str]:
    """Distinct authors of finished books, as 'First Last' display names."""
    rows = (
        db.query(Book.author_name_first, Book.author_name_second)
        .join(Reading, Reading.book_id == Book.id)
        .filter(Reading.date_finished_actual.isnot(None))
        .distinct()
        .all()
    )
    names, seen = [], set()
    for first, second in rows:
        name = " ".join(p for p in (first, second) if p).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def get_watch(db: Session) -> dict:
    """The watch set: auto-derived read authors, plus user include/exclude layers."""
    auto = _read_authors(db)
    excluded = _get_list_setting(db, EXCLUDED_KEY)
    extra = _get_list_setting(db, EXTRA_KEY)
    excl_l = {e.lower() for e in excluded}
    effective, seen = [], set()
    for name in [*auto, *extra]:
        if name.lower() in excl_l or name.lower() in seen:
            continue
        seen.add(name.lower())
        effective.append(name)
    effective.sort(key=str.lower)
    return {"auto": auto, "excluded": excluded, "extra": extra, "effective": effective}


def add_excluded(db: Session, author: str) -> None:
    _set_list_setting(db, EXCLUDED_KEY, [*_get_list_setting(db, EXCLUDED_KEY), author])


def remove_excluded(db: Session, author: str) -> None:
    cur = _get_list_setting(db, EXCLUDED_KEY)
    _set_list_setting(db, EXCLUDED_KEY, [a for a in cur if a.lower() != author.lower()])


def add_extra(db: Session, author: str) -> None:
    _set_list_setting(db, EXTRA_KEY, [*_get_list_setting(db, EXTRA_KEY), author])


def remove_extra(db: Session, author: str) -> None:
    cur = _get_list_setting(db, EXTRA_KEY)
    _set_list_setting(db, EXTRA_KEY, [a for a in cur if a.lower() != author.lower()])


# ── pipeline helpers ────────────────────────────────────────────────────────
def _parse_pub_date(s: Optional[str]) -> tuple[Optional[date], Optional[str]]:
    """'YYYY-MM-DD'|'YYYY-MM'|'YYYY' → (date, precision). Year/month-only snap to 1st."""
    if not s:
        return None, None
    parts = s.split("-")
    try:
        if len(parts) >= 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2])), "day"
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1), "month"
        if len(parts) == 1 and parts[0]:
            return date(int(parts[0]), 1, 1), "year"
    except (ValueError, IndexError):
        return None, None
    return None, None


def _author_matches(watched: str, primary: Optional[str]) -> bool:
    """Stage 2: the watched author must be the book's primary author (house-style tolerant)."""
    if not primary:
        return False
    wt, pt = _word_tokens(watched), _word_tokens(primary)
    if not wt or not pt:
        return False
    surname = _word_tokens(watched.strip().split()[-1]) if watched.strip() else frozenset()
    if surname and not surname <= pt:   # surname must appear in the book's author
        return False
    union = len(wt | pt)
    return union > 0 and len(wt & pt) / union >= 0.6


# Edition qualifiers that shouldn't split a work into separate cards.
_EDITION_RE = re.compile(
    r"\(.*?\)"                                    # any parenthetical, e.g. "(Standard Edition)"
    r"|\b(?:deluxe|standard|limited|collector'?s|special|illustrated|anniversary|expanded"
    r"|revised|international|exclusive|signed|hardcover|paperback|reprint)\b"
    r"|\bedition\b",
    re.IGNORECASE,
)


def _canonical_title(title: str) -> str:
    """Strip edition qualifiers so variant editions collapse to one work."""
    return _EDITION_RE.sub(" ", title or "")


def _work_key(title: str, author: str) -> str:
    return (" ".join(sorted(_word_tokens(_canonical_title(title)))) + "|"
            + " ".join(sorted(_word_tokens(author))))


def _match_series(title: str, series_names: list[str]) -> Optional[str]:
    """Best-effort series tag: an existing series whose tokens are fully inside the title."""
    tt = _word_tokens(title)
    best = None
    for s in series_names:
        st = _word_tokens(s)
        if st and st <= tt and (best is None or len(st) > len(_word_tokens(best))):
            best = s
    return best


def _detect_category(title: str, subtitle: Optional[str], categories, author: str) -> str:
    """'comic' for comics/graphic novels/manga/licensed adaptations, else 'book'."""
    cats = " ".join(categories or []).lower()
    if "comics & graphic novels" in cats:
        return "comic"
    blob = " ".join(filter(None, [title, subtitle]))
    if _COMIC_RE.search(blob or ""):
        return "comic"
    # Licensed adaptation: "Andrzej Sapkowski's The Witcher: ..." — the watched author's
    # name in a possessive prefix marks a tie-in/comic, not a novel they wrote.
    if title and "'s " in title:
        surname = author.strip().split()[-1].lower() if author.strip() else ""
        if surname and surname in title.split("'s ")[0].lower():
            return "comic"
    return "book"


# Title sanitization + parsing ------------------------------------------------
_WORD_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
# A trailing binding/edition code: "_2 Hb", " Hb", " Pb", " Hardcover", etc.
_FORMAT_CODE_RE = re.compile(
    r"\s*[_\-]\s*\d+\s*$|\s+\b(?:hb|pb|hc|tpb|hardback|hardcover|paperback|mass\s*market)\b\.?\s*$",
    re.IGNORECASE,
)
# Trailing edition qualifiers in bracket/colon/qualifier form: "[Special Deluxe Edition]",
# "Credence: Deluxe Edition", "… Collector's Edition".
_EDITION_SUFFIX_RE = re.compile(
    r"\s*[\[(][^\])]*\bedition\b[^\])]*[\])]\s*$"
    r"|\s*[:\-]\s*(?:\w+\s+){0,3}edition\b\s*$"
    r"|\s+(?:deluxe|standard|limited|special|collector'?s|illustrated|anniversary|expanded"
    r"|revised|international|exclusive|signed)\s+edition\b\s*$",
    re.IGNORECASE,
)


def _clean_title(title: Optional[str]) -> Optional[str]:
    """Strip trailing binding/format codes, edition qualifiers, and underscores."""
    if not title:
        return title
    t, prev = title, None
    while prev != t:                       # peel repeated codes: "Foo_2 Hb", "X: Deluxe Edition"
        prev = t
        t = _FORMAT_CODE_RE.sub("", t).strip()
        t = _EDITION_SUFFIX_RE.sub("", t).strip()
    t = re.sub(r"\s{2,}", " ", t.replace("_", " ")).strip(" :,-")
    return t or title


def _title_is_messy(title: Optional[str]) -> bool:
    return bool(title) and (("_" in title) or bool(re.search(r"\b(?:hb|pb)\b", title, re.IGNORECASE)))


def _best_display_title(candidates: list) -> Optional[str]:
    """Pick the cleanest, most complete title across a work's editions: prefer no
    parenthetical edition qualifier — "The Sweetest Fiend" over "… (Deluxe Edition)"."""
    pool = [c for c in candidates if c and not _title_is_messy(c)] or [c for c in candidates if c]
    if not pool:
        return None
    return max(pool, key=lambda t: (0 if "(" in t else 1, 1 if ":" in t else 0, len(t)))


# Non-English detection / extraction -------------------------------------------
_ACCENTS = set("àáâãäåçèéêëìíîïñòóôõöùúûüýÿ")
# Distinctive non-English tokens — deliberately avoid English homographs (as, a, o, is).
_FOREIGN_WORDS = {
    "el", "la", "los", "las", "un", "una", "del", "della", "dello", "di", "il", "lo",
    "per", "que", "mi", "vida", "novela", "para", "entre", "rio", "estrelas", "historia",
    "superviviente", "quiere", "morir", "innamorarsi", "pasticcio", "bel", "caso",
    "amore", "amour", "livre", "edicion", "edición", "auf", "der", "die", "das", "und",
    "muerte", "sangre", "corazon", "corazón", "cielo", "noche", "guerra", "mundo", "vie",
}
_LANG_PAREN_RE = re.compile(
    r"\s*\((?:english|spanish|french|german|italian|portuguese|edici[oó]n|édition)[^)]*\)",
    re.IGNORECASE)
_NOVEL_SUFFIX_RE = re.compile(
    r"\s*[:,]\s*(?:a\s+)?(?:novel|novela|roman|romanzo|a\s+novel)\s*$", re.IGNORECASE)


def _foreign_score(text: Optional[str]) -> int:
    if not text:
        return 0
    s = 2 if any(c in _ACCENTS for c in text.lower()) else 0
    return s + sum(1 for w in re.findall(r"[a-zà-ÿ]+", text.lower()) if w in _FOREIGN_WORDS)


def _englishify_title(title: Optional[str]) -> Optional[str]:
    """Clean to an English title; return None if it's a non-English edition.

    Handles "(English Edition)" tags, bilingual "Foreign / English" titles (keeps the
    English side), and ": A Novel"/"Novela" suffixes; drops titles that stay non-English.
    """
    if not title:
        return title
    t = _LANG_PAREN_RE.sub("", title).strip()
    if " / " in t:                                  # bilingual → keep the English side
        t = min(t.split(" / "), key=_foreign_score).strip()
    t = _NOVEL_SUFFIX_RE.sub("", t).strip()
    t = _clean_title(t)
    if t and _foreign_score(t) >= 2:                # still clearly non-English → drop
        return None
    return t


def _parse_series_number(title: Optional[str]) -> Optional[float]:
    """Pull a book number from 'Book 2' / 'Vol. 3' / '#4' / 'Book Two' / '_2'."""
    if not title:
        return None
    m = re.search(r"\b(?:book|vol\.?|volume|part|#)\s*(\d+(?:\.\d+)?)\b", title, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r"\bbook\s+([a-z]+)\b", title, re.IGNORECASE)
    if m and m.group(1).lower() in _WORD_NUMS:
        return float(_WORD_NUMS[m.group(1).lower()])
    m = re.search(r"[_#]\s*(\d+)\b", title)
    return float(m.group(1)) if m else None


def _primary_genre(categories) -> Optional[str]:
    """A specific genre from Google's BISAC categories (prefer non-generic)."""
    parts = []
    for c in (categories or []):
        parts += [p.strip() for p in re.split(r"\s*/\s*", c) if p.strip()]
    for p in parts:
        if p.lower() not in {"fiction", "general", "nonfiction"}:
            return p
    return parts[0] if parts else None


def _enrich_from_editions(client, clean_title: str, author: str, rep: dict, editions: list):
    """One extra Google query for this exact work → (earliest_year, best_title, best_thumbnail).

    Lets us flag reprints of old books even when the author search only returned the new
    edition, fill a missing cover from a sibling edition, and pick a clean title.
    """
    eds = client.get_editions(clean_title, author)
    years = [e["_date"].year for e in editions if e.get("_date")]
    years += [e.get("year") for e in eds if e.get("year")]
    min_year = min(years) if years else (rep["_date"].year if rep.get("_date") else None)
    thumb = (rep.get("thumbnail")
             or next((e.get("thumbnail") for e in editions if e.get("thumbnail")), None)
             or next((e.get("thumbnail") for e in eds if e.get("thumbnail")), None))
    best_title = _best_display_title([e.get("title") for e in editions]
                                     + [e.get("title") for e in eds])
    return min_year, best_title, thumb


def _classify(rep_date: date, today: date) -> Optional[str]:
    delta = (rep_date - today).days
    if delta > 0:
        return "upcoming" if delta <= UPCOMING_MAX_DAYS else None
    if delta >= -NEW_WINDOW_DAYS:
        return "new"
    return None


# ── main poll ────────────────────────────────────────────────────────────────
def poll_releases(db: Session) -> dict:
    """Poll Google Books for every watched author and upsert survivors. Returns a summary."""
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if not api_key:
        logger.warning("news poll: GOOGLE_BOOKS_API_KEY not set — keyless quota is 0, aborting.")
        return {"ok": False, "error": "no_api_key"}

    client = GoogleBooksClient(api_key=api_key)
    today = date.today()

    watch = get_watch(db)["effective"]
    existing_books = _build_existing(db)
    series_names = [r[0] for r in db.query(Book.series).filter(Book.series.isnot(None)).distinct().all()]
    # "Owned" = you have a copy (inventory row) or finished reading it. Books in the DB
    # that are neither are "tracked" (pre-added/on your radar) — surfaced, not dropped.
    owned_ids = {bid for (bid,) in db.query(Inventory.book_id).distinct() if bid}
    owned_ids |= {bid for (bid,) in db.query(Reading.book_id)
                  .filter(Reading.date_finished_actual.isnot(None)).distinct() if bid}
    dismissed_keys = {
        wk for (wk,) in db.query(NewsItem.work_key).filter(NewsItem.dismissed.is_(True)).all() if wk
    }

    drops: dict[str, int] = {}
    def _drop(stage: str) -> None:
        drops[stage] = drops.get(stage, 0) + 1

    surfaced = 0
    processed_gids: set = set()   # a co-authored book can surface under 2 watched authors
    for author in watch:
        try:
            raw = client.search_by_author(author, max_results=MAX_RESULTS_PER_AUTHOR)
        except Exception as exc:                       # 429/network — skip author, keep cache
            logger.warning("news poll: search failed for %s: %s", author, exc)
            continue

        # Stages 1–3 + parse, then group editions by work.
        works: dict[str, list[dict]] = {}
        for b in raw:
            lang = (b.get("language") or "").lower()
            if lang and lang != "en":
                _drop("1_language"); continue
            if not _author_matches(author, b.get("primary_author")):
                _drop("2_author"); continue
            blob = " ".join(filter(None, [b.get("title"), b.get("subtitle")]))
            if _JUNK_RE.search(blob or ""):
                _drop("3_junk"); continue
            d, prec = _parse_pub_date(b.get("published_date"))
            if not d:
                _drop("7_no_date"); continue
            b["_date"], b["_precision"] = d, prec
            works.setdefault(_work_key(b.get("title", ""), author), []).append(b)

        for wk, editions in works.items():
            if wk in dismissed_keys:
                _drop("9_dismissed"); continue
            # Representative edition: prefer a clean (no-parenthetical) title, then
            # complete metadata (cover + ISBN), then the newest date.
            rep = max(editions, key=lambda e: (
                0 if "(" in (e.get("title") or "") else 1,
                1 if (e.get("thumbnail") and e.get("isbn_13")) else 0,
                e["_date"].toordinal(),
            ))
            kind = _classify(rep["_date"], today)
            if not kind:
                _drop("5_window"); continue

            # Clean the title FIRST (foreign/binding/edition) so the DB-ownership match and
            # all filters use the real title — "Tapestry of Fate_2 Hb" must match the owned
            # "The Tapestry of Fate". Drop non-English here, before the paid edition lookup.
            clean_title = _englishify_title(_clean_title(rep.get("title")))
            if not clean_title:
                _drop("1b_foreign"); continue

            # Match the cleaned title against the DB. Owned (copy or finished) → drop;
            # in-DB but unowned → tracked; not in DB → a fresh discovery.
            owned = _best_match(clean_title, author, None, None,
                                str(rep["_date"].year), existing_books)
            tracked, matched_id = False, None
            if owned:
                matched_id = owned["id"]
                if matched_id in owned_ids:
                    _drop("8_owned"); continue
                tracked = True

            gid = rep.get("google_books_id")
            if not gid:
                _drop("no_gid"); continue
            if gid in processed_gids:        # same edition already surfaced this run
                continue
            processed_gids.add(gid)

            # Capture the raw record before cleaning, then enrich from sibling editions
            # (earliest-year for reprint detection, cover fallback, clean title).
            raw_json = json.dumps({k: v for k, v in rep.items() if not k.startswith("_")})
            min_year, best_title, thumb = _enrich_from_editions(client, clean_title, author, rep, editions)
            display_title = _englishify_title(_clean_title(best_title)) or clean_title
            rep_year = rep["_date"].year
            rep["title"], rep["thumbnail"] = display_title, thumb

            # Independent flags (a book can be both a comic and a reprint → AND filtering).
            is_comic = _detect_category(display_title, rep.get("subtitle"),
                                        rep.get("categories"), author) == "comic"
            is_reprint = bool(min_year and (rep_year - min_year) >= REPRINT_MIN_AGE_YEARS)
            category = "comic" if is_comic else ("reprint" if is_reprint else "book")
            genre = _primary_genre(rep.get("categories"))
            series_number = (rep.get("series_number") or _parse_series_number(display_title)
                             or _parse_series_number(rep.get("subtitle")))
            series_tag = _match_series(display_title, series_names)
            low_conf = not (thumb and rep.get("isbn_13"))
            _upsert(db, author, rep, kind, low_conf, series_tag, wk,
                    category=category, tracked=tracked, matched_id=matched_id,
                    genre=genre, series_number=series_number, raw_json=raw_json,
                    is_comic=is_comic, is_reprint=is_reprint)
            surfaced += 1

    db.commit()
    deduped = _dedup_untitled(db) + _dedup_crossentry(db)
    logger.info("news poll done: %d authors, %d surfaced, %d deduped, drops=%s",
                len(watch), surfaced, deduped, drops)
    return {"ok": True, "authors": len(watch), "surfaced": surfaced, "deduped": deduped, "drops": drops}


def _dedup_untitled(db: Session) -> int:
    """Drop 'Untitled … Book N' placeholders when a real-titled edition of the same
    book (same author, pub date within ~120 days) is also present."""
    removed = 0
    for u in db.query(NewsItem).filter(NewsItem.title.ilike("untitled%")).all():
        if not u.published_date:
            continue
        lo, hi = u.published_date - timedelta(days=120), u.published_date + timedelta(days=120)
        sib = (db.query(NewsItem)
               .filter(NewsItem.id != u.id, NewsItem.author_name == u.author_name,
                       ~NewsItem.title.ilike("untitled%"),
                       NewsItem.published_date.between(lo, hi)).first())
        if sib:
            db.delete(u)
            removed += 1
    if removed:
        db.commit()
    return removed


_STOPWORDS = {"the", "of", "a", "an", "and", "to", "in"}


def _sig_tokens(title: Optional[str]) -> list:
    toks = re.sub(r"[^\w\s]", " ", (title or "").lower()).split()
    return [w for w in toks if w not in _STOPWORDS]


def _dup_score(it) -> tuple:
    return (1 if it.thumbnail_url else 0, 1 if it.isbn_13 else 0, len(it.title or ""))


def _dedup_crossentry(db: Session) -> int:
    """Collapse only true prefix-extensions of the same work — same author where one
    title's significant words are a *prefix* of the other's: 'Legacies of Betrayal' vs
    'Legacies of Betrayal: The Third Tale of Witness'. Distinct series entries that merely
    share a prefix ('Lucky Starr and the Oceans of Venus' vs '… Pirates of the Asteroids')
    diverge and are NOT merged. Keeps the entry with the best metadata."""
    by_author: dict = {}
    for it in db.query(NewsItem).all():
        sig = tuple(_sig_tokens(it.title))
        if len(sig) >= 2:
            by_author.setdefault((it.author_name or "").lower(), []).append((it, sig))
    removed = 0
    for items in by_author.values():
        items.sort(key=lambda x: (len(x[1]), x[0].id))   # shortest title first
        kept: list = []
        for it, sig in items:
            dup_idx = None
            for idx, (_, ksig) in enumerate(kept):
                short, lng = (ksig, sig) if len(ksig) <= len(sig) else (sig, ksig)
                if lng[:len(short)] == short:            # one is a prefix of the other
                    dup_idx = idx
                    break
            if dup_idx is None:
                kept.append((it, sig))
            else:
                kit = kept[dup_idx][0]
                if _dup_score(it) > _dup_score(kit):
                    db.delete(kit)
                    kept[dup_idx] = (it, sig)
                else:
                    db.delete(it)
                removed += 1
    if removed:
        db.commit()
    return removed


def reprocess(db: Session) -> dict:
    """Re-run title cleanup + language/junk filters + dedup over EXISTING rows using
    stored raw_json — NO API calls. Lets us iterate filtering without spending quota."""
    existing_books = _build_existing(db)
    owned_ids = {bid for (bid,) in db.query(Inventory.book_id).distinct() if bid}
    owned_ids |= {bid for (bid,) in db.query(Reading.book_id)
                  .filter(Reading.date_finished_actual.isnot(None)).distinct() if bid}
    removed = updated = 0
    for item in db.query(NewsItem).all():
        try:
            raw = json.loads(item.raw_json) if item.raw_json else {}
        except (ValueError, TypeError):
            raw = {}
        orig_title = raw.get("title") or item.title
        blob = " ".join(filter(None, [orig_title, raw.get("subtitle")]))
        eng = _englishify_title(orig_title)
        if (blob and _JUNK_RE.search(blob)) or not eng:      # combo/set or non-English → drop
            db.delete(item)
            removed += 1
            continue
        # Re-check ownership on the cleaned title (catches books already in the library
        # whose raw title was too messy to match at poll time, e.g. Tapestry of Fate).
        yr = str(item.published_date.year) if item.published_date else None
        owned = _best_match(eng, item.author_name, None, None, yr, existing_books)
        if owned:
            if owned["id"] in owned_ids:
                db.delete(item)
                removed += 1
                continue
            item.tracked = True
            item.matched_book_id = owned["id"]
        item.title = eng
        item.genre = _primary_genre(raw.get("categories"))
        if item.series_number is None:
            item.series_number = (raw.get("series_number")
                                  or _parse_series_number(eng) or _parse_series_number(raw.get("subtitle")))
        item.is_comic = _detect_category(eng, raw.get("subtitle"), raw.get("categories"), item.author_name) == "comic"
        item.category = "comic" if item.is_comic else ("reprint" if item.is_reprint else "book")
        updated += 1
    db.commit()
    removed += _dedup_untitled(db)
    removed += _dedup_crossentry(db)
    return {"removed": removed, "updated": updated, "remaining": db.query(NewsItem).count()}


def enrich_with_openlibrary(db: Session, only_missing: bool = True) -> dict:
    """Cross-reference each news item against OpenLibrary (#69) — no Google quota.

    Adds: first-publish-year → reprint reclassification (catches pseudonym/old works
    like Asimov's Lucky Starr), binding (HC/PB), a cover when Google had none, and a
    word-count estimate (~300 wpp) from page_count.
    """
    from ..discovery.openlibrary_client import OpenLibraryClient
    client = OpenLibraryClient()
    today_year = date.today().year
    n_reprint = n_bind = n_cover = n_words = 0
    items = db.query(NewsItem).all()
    for item in items:
        fpy = item.first_publish_year
        if fpy is None:
            fpy = client.first_publish_year(item.title, item.author_name)
            if fpy:
                item.first_publish_year = fpy
        rep_year = item.published_date.year if item.published_date else today_year
        if fpy and not item.is_reprint and (rep_year - fpy) >= REPRINT_MIN_AGE_YEARS:
            item.is_reprint = True
            if item.category == "book":
                item.category = "reprint"
            n_reprint += 1

        isbn = item.isbn_13 or item.isbn_10
        pages = None
        if isbn and (not only_missing or not item.binding or not item.thumbnail_url):
            ed = client.edition_by_isbn(isbn)
            pages = ed.get("pages")
            if ed.get("binding") and not item.binding:
                item.binding = ed["binding"]; n_bind += 1
            if ed.get("cover_url") and not item.thumbnail_url:
                item.thumbnail_url = ed["cover_url"]; item.low_confidence = not item.isbn_13; n_cover += 1

        if item.word_count is None:
            if pages is None and item.raw_json:
                try:
                    pages = json.loads(item.raw_json).get("page_count")
                except (ValueError, TypeError):
                    pages = None
            if isinstance(pages, int) and pages > 0:
                item.word_count = pages * 300; n_words += 1
    db.commit()
    return {"total": len(items), "reprints+": n_reprint, "binding+": n_bind,
            "covers+": n_cover, "words+": n_words}


def _upsert(db, author, rep, kind, low_conf, series_tag, work_key,
           category="book", tracked=False, matched_id=None,
           genre=None, series_number=None, raw_json=None,
           is_comic=False, is_reprint=False) -> None:
    gid = rep.get("google_books_id")
    if not gid:
        return
    item = db.query(NewsItem).filter(NewsItem.google_books_id == gid).first()
    now = datetime.utcnow()
    fields = dict(
        work_key=work_key, author_name=author, title=rep.get("title"),
        subtitle=rep.get("subtitle"), published_date=rep["_date"], date_precision=rep["_precision"],
        isbn_13=rep.get("isbn_13"), isbn_10=rep.get("isbn_10"), thumbnail_url=rep.get("thumbnail"),
        preview_link=rep.get("preview_link"), matched_series=series_tag,
        series_number=series_number, genre=genre, matched_book_id=matched_id,
        tracked=tracked, category=category, is_comic=is_comic, is_reprint=is_reprint,
        kind=kind, low_confidence=low_conf, raw_json=raw_json, last_polled_at=now,
    )
    if item:
        for k, v in fields.items():       # refresh data; never touch seen/dismissed
            setattr(item, k, v)
    else:
        db.add(NewsItem(google_books_id=gid, discovered_at=now, seen=False, dismissed=False, **fields))


# ── feed reads / mutations ───────────────────────────────────────────────────
def list_news(db: Session, kind: Optional[str] = None, include_low: bool = True) -> list[dict]:
    """Non-dismissed items. Upcoming sorted soonest-first, new sorted most-recent-first."""
    q = db.query(NewsItem).filter(NewsItem.dismissed.is_(False))
    if kind in ("upcoming", "new"):
        q = q.filter(NewsItem.kind == kind)
    if not include_low:
        q = q.filter(NewsItem.low_confidence.is_(False))
    items = q.all()
    upcoming = sorted([i for i in items if i.kind == "upcoming"],
                      key=lambda i: i.published_date or date.max)
    new = sorted([i for i in items if i.kind == "new"],
                 key=lambda i: i.published_date or date.min, reverse=True)
    return [i.to_dict() for i in (upcoming + new)]


def unread_count(db: Session) -> int:
    return (
        db.query(NewsItem)
        .filter(NewsItem.dismissed.is_(False), NewsItem.seen.is_(False))
        .count()
    )


def mark_seen(db: Session, item_id: Optional[int] = None) -> None:
    q = db.query(NewsItem).filter(NewsItem.seen.is_(False))
    if item_id is not None:
        q = q.filter(NewsItem.id == item_id)
    for i in q.all():
        i.seen = True
    db.commit()


def _owned_book_id_subq(db: Session):
    """Subquery of book_ids that have an owned copy in any format."""
    return (db.query(Inventory.book_id)
            .filter(or_(Inventory.owned_physical.is_(True),
                        Inventory.owned_ebook.is_(True),
                        Inventory.owned_audio.is_(True)))
            .distinct())


def _remote_card(i: "NewsItem", status: str) -> dict:
    return {
        "kind": "remote", "status": status, "news_id": i.id, "book_id": i.matched_book_id,
        "title": i.title, "author": i.author_name, "series": i.matched_series,
        "series_number": i.series_number, "cover_url": i.thumbnail_url, "has_cover": bool(i.thumbnail_url),
        "is_comic": i.is_comic, "is_reprint": i.is_reprint, "tracked": i.tracked,
        "word_count": i.word_count, "genre": i.genre, "binding": i.binding,
        "date": i.published_date.isoformat() if i.published_date else None,
        "date_precision": i.date_precision, "first_publish_year": i.first_publish_year,
        "preview_link": i.preview_link, "read_count": None,
    }


def _local_card(b: Book, status: str, read_count: int) -> dict:
    cover_version = 0
    if b.cover:
        try:
            cover_version = int((settings.covers_dir / f"{b.id}.jpg").stat().st_mtime)
        except OSError:
            cover_version = 0
    return {
        "kind": "local", "status": status, "news_id": None, "book_id": b.id,
        "title": b.title, "author": b.author, "series": b.series, "series_number": b.series_number,
        "cover_url": None, "has_cover": bool(b.cover), "cover_version": cover_version,
        "is_comic": False, "is_reprint": False,
        "tracked": status == "unowned", "word_count": b.word_count, "page_count": b.page_count,
        "genre": b.genre, "binding": None,
        "date": b.date_published.isoformat() if b.date_published else None,
        "date_precision": "day", "first_publish_year": None, "preview_link": None,
        "read_count": read_count,
    }


def list_shelf(db: Session, status: str = "owned", search: str = None,
               skip: int = 0, limit: int = 60, sort_by: str = "author",
               sort_order: str = "asc", cover: str = "all") -> dict:
    """Unified Books-page feed (#88): normalized cards for one status.
    owned/unowned come from the DB (split by `inv` ownership); upcoming/new from news_items."""
    status = (status or "owned").lower()
    like = f"%{search.strip()}%" if search and search.strip() else None
    desc = (sort_order or "asc").lower() == "desc"
    sort_by = (sort_by or "author").lower()

    if status in ("upcoming", "new"):
        q = db.query(NewsItem).filter(NewsItem.dismissed.is_(False), NewsItem.kind == status)
        if like:
            q = q.filter(or_(NewsItem.title.ilike(like), NewsItem.author_name.ilike(like),
                             NewsItem.matched_series.ilike(like)))
        items = q.all()
        if cover == "yes":
            items = [i for i in items if i.thumbnail_url]
        elif cover == "no":
            items = [i for i in items if not i.thumbnail_url]
        keyf = {
            "title": lambda i: (i.title or "").lower(),
            "author": lambda i: (i.author_name or "").lower(),
            "series": lambda i: ((i.matched_series or "").lower(), i.series_number or 0),
            "date": lambda i: i.published_date or date.min,
            "words": lambda i: i.word_count or 0,
        }.get(sort_by, lambda i: i.published_date or date.min)
        # default date direction differs per kind; explicit sort_order wins
        rev = desc if sort_by in ("title", "author", "series", "date", "words") else (status == "new")
        items.sort(key=keyf, reverse=rev)
        total = len(items)
        cards = [_remote_card(i, status) for i in items[skip:skip + limit]]
        return {"status": status, "total": total, "cards": cards}

    owned_ids = _owned_book_id_subq(db)
    q = db.query(Book)
    q = q.filter(Book.id.in_(owned_ids)) if status == "owned" else q.filter(~Book.id.in_(owned_ids))
    if like:
        q = q.filter(or_(Book.title.ilike(like), Book.author_name_first.ilike(like),
                         Book.author_name_second.ilike(like), Book.series.ilike(like)))
    if cover == "yes":
        q = q.filter(Book.cover.is_(True))
    elif cover == "no":
        q = q.filter(Book.cover.is_(False))
    total = q.count()
    sort_cols = {
        "title": [Book.title], "author": [Book.author_name_second, Book.author_name_first],
        "series": [Book.series, Book.series_number], "date": [Book.date_published],
        "words": [Book.word_count],
    }.get(sort_by, [Book.author_name_second, Book.author_name_first])
    order = [(c.desc() if desc else c.asc()) for c in sort_cols] + [Book.title]
    books = q.order_by(*order).offset(skip).limit(limit).all()
    # read-counts for this page in one query
    ids = [b.id for b in books]
    counts = {}
    if ids:
        for bid, c in (db.query(Reading.book_id, func.count(Reading.id))
                       .filter(Reading.book_id.in_(ids), Reading.date_finished_actual.isnot(None))
                       .group_by(Reading.book_id).all()):
            counts[bid] = c
    cards = [_local_card(b, status, counts.get(b.id, 0)) for b in books]
    return {"status": status, "total": total, "cards": cards}


def author_finished_books(db: Session, author_name: str) -> list[dict]:
    """Books the user has finished by this author — for the News card's detail popup."""
    first, second = _split_author(author_name or "")
    q = (db.query(Book).join(Reading, Reading.book_id == Book.id)
         .filter(Reading.date_finished_actual.isnot(None)))
    if second:
        q = q.filter(Book.author_name_second.ilike(second))
    target = _word_tokens(author_name)
    by_id: dict = {}
    for b in q.all():
        full = " ".join(p for p in (b.author_name_first, b.author_name_second) if p)
        if target and _word_tokens(full) != target:      # exact author match (house-style tolerant)
            continue
        by_id[b.id] = {
            "id": b.id, "title": b.title, "series": b.series,
            "series_number": b.series_number, "has_cover": bool(b.cover),
        }
    books = list(by_id.values())
    books.sort(key=lambda x: (x["series"] or "~", x["series_number"] or 0, x["title"] or ""))
    return books


def dismiss(db: Session, item_id: int) -> bool:
    item = db.query(NewsItem).filter(NewsItem.id == item_id).first()
    if not item:
        return False
    item.dismissed = True
    db.commit()
    return True
