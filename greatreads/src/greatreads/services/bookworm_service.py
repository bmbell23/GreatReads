"""Bookworm: post book automation to #greatreads in Mattermost (#320).

Every GreatReads book action already goes through ``log_event`` (#184), so that is
the one hook: ``log_event`` hands each event to :func:`notify`, which filters it,
words it, and queues it for the **bookworm** bot. One thread per book (keyed by
normalised title), so a book's journey reads top to bottom: borrow → .acsm →
Calibre/ABS link → metadata → cover. The thread's root is a fixed "Book Report"
header and every event, the first included, is a reply under it (#323).

Rules:
- **Starts a thread:** Libby events, import links and Calibre watcher steps (something
  new happened). A thread whose root is older than ``STALE_THREAD_HOURS`` is not reused:
  the next starting event opens a fresh Book Report, so it shows in the channel instead
  of hiding as a reply under a day-old root (#351).
- **Reply-only:** metadata enrichment, cover upgrades and MediaForge's chapter /
  sync-map steps (``media``). They only post into a
  book's thread that is already open, so the nightly backfill over the whole
  library never floods the channel (and never even queues).
- **Never:** ``device/*`` (phone telemetry), ``system/*`` (deploys are Biscuit's),
  ``import/dismiss``.

Best-effort and non-blocking like ``log_event``: one daemon worker drains a bounded
queue (full → drop), and every error is swallowed. No ``BOOKWORM_TOKEN`` → off.
The app runs a single process, so the in-memory thread map is authoritative; the
JSON file only carries it across restarts.
"""

import json
import logging
import os
import queue
import re
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

MM_URL = os.environ.get("BOOKWORM_MM_URL", "http://host.docker.internal:8015").rstrip("/")
MM_TEAM = os.environ.get("BOOKWORM_TEAM", "office")
MM_CHANNEL = os.environ.get("BOOKWORM_CHANNEL", "greatreads")
THREADS_FILE = os.environ.get(
    "BOOKWORM_THREADS_FILE",
    os.path.join(os.environ.get("EREADER_DATA_DIR", "/app/data"), "bookworm_threads.json"),
)
try:
    # A book's thread stays open this long after its last post; later events start fresh.
    THREAD_TTL_S = float(os.environ.get("BOOKWORM_THREAD_TTL_H", "72")) * 3600
except ValueError:
    THREAD_TTL_S = 72 * 3600
try:
    # A root older than this (from its creation, not its last reply) is stale (#351).
    STALE_THREAD_S = float(os.environ.get("STALE_THREAD_HOURS", "12")) * 3600
except ValueError:
    STALE_THREAD_S = 12 * 3600

HEADER = "📚 Starting a new Book Report for {t}. See thread for more details."

# (category, event) → message. ``{t}`` is the bolded title, ``{fmt}`` "ebook" or
# "audiobook", ``{lib}`` " from <library>" (empty when unknown). It's all Libby, so
# the lines don't say so. Unlisted events get a generic line in "starts" categories
# and are dropped otherwise.
MESSAGES = {
    ("libby", "auto_borrow"): "🤖 Hold was ready, so I borrowed {t} on {fmt}{lib}!",
    ("libby", "borrow_start"): "📥 Borrowing {t} on {fmt}{lib}…",
    ("libby", "borrow"): "✅ Borrowed {t} on {fmt}{lib}!",
    ("libby", "borrow_failed"): "⚠️ Couldn't borrow {t} on {fmt}{lib}",
    ("libby", "hold_claimed"): "🎟️ Claimed the ready hold on {t} on {fmt}{lib}!",
    ("libby", "acsm_download"): "📄 Got the .acsm for {t}; the Calibre watcher takes it from here",
    ("libby", "download_failed"): "⚠️ Borrowed {t} but the .acsm download failed",
    ("libby", "return"): "↩️ Returned {t}{lib} (import confirmed)",
    ("libby", "autofulfill_parked"): "⏸️ Auto-fulfill parked {t} after repeated failures",
    ("libby", "autofulfill_giveup"): "🛑 Gave up confirming the import of {t}; the loan is kept",
    ("libby", "audiobook_download_start"): "🎧 Downloading {t} on audiobook{lib}…",
    ("libby", "audiobook_borrow_download_start"): "🎧 Borrowing {t} on audiobook{lib} and downloading it…",
    ("libby", "audiobook_download"): "🎧 Downloaded {t} on audiobook{lib}!",
    ("libby", "audiobook_download_failed"): "⚠️ Audiobook download failed for {t}",
    ("libby", "audiobook_borrow_manual"): "✋ Borrowed {t} on audiobook{lib}; it needs a manual harvest",
    # Calibre watcher steps, ingested via POST /api/events/ingest (#351)
    ("calibre", "picked_up"): "📥 Picked up {t} from the downloads folder",
    ("calibre", "added"): "📚 {t} added to Calibre (#{calibre_id})",
    ("calibre", "finalized"): "🧮 {t}: {words} words{series}",
    ("calibre", "failed"): "⚠️ Calibre couldn't import {t}: {reason}",
    ("calibre", "skipped_duplicate"): "♻️ {t} is already in Calibre",
    ("import", "linked"): "🔗 {t} from {src} linked into GreatReads",
    ("import", "created"): "📚 {t} from {src} added to GreatReads",
    ("metadata", "enriched"): "🏷️ Metadata filled in for {t}: {fields}",
    ("cover", "upgraded"): "🖼️ Better cover for {t} ({from_px}px → {to_px}px)",
    # MediaForge's answers to our requests (mediaforge_service, #326)
    ("media", "chapters_fixed"): "🧩 Chapters fixed for {t}{text}",
    ("media", "chapters_ok"): "👌 Chapters fine as-is for {t}{text}",
    ("media", "chapters_already_fixed"): "👌 Chapters already fixed for {t}{text}",
    ("media", "chapters_skipped"): "⏭️ Chapters left alone for {t}{text}",
    ("media", "chapters_parked"): "⏸️ Chapter fix parked for {t}{text}",
    ("media", "sync_map_built"): "🔄 Sync map ready for {t}{text}",
    ("media", "sync_map_skipped"): "⏭️ No sync map for {t}{text}",
    ("media", "sync_map_failed"): "⚠️ Sync map failed for {t}{text}",
    ("media", "unknown"): "❓ MediaForge couldn't find the audiobook of {t} in ABS{text}",
}
# Wishlist auto-acquire (#297/#349): one line per outcome, keyed on the event's detail
# ``status``. Every attempt posts. Extra fields: ``{n}`` " (checked N results)" and
# ``{why}`` a short reason; statuses using ``{why}`` skip the generic "> reason" quote.
WISHLIST_ACQUIRE = {
    "acquired": "🤖 Borrowed {t} from your Wishlist{lib}",
    "acquired_audiobook": "🎧 Borrowing {t} on audiobook from your Wishlist{lib}; the harvest is running",
    "acquired_existing": "✅ {t} was already on loan{lib}; it's queued for import and return",
    "on_hold": "🎟️ Placed a hold on {t}{lib}",
    "on_hold_existing": "🎟️ {t} is already on hold",
    "no_match": "🔎 {t} isn't on Libby{n}",
    "unavailable": "🚫 {t} isn't in any of our libraries",
    "at_hold_limit": "⏳ {t}: cards are full",
    "at_loan_limit": "⏳ {t}: cards are full",
    "format_blocked": "🚧 {t}: no card can download that format automatically",
    "failed": "⚠️ Couldn't get {t}: {why}",
    "engine_unreachable": "📡 Couldn't reach Libby while trying {t}; I'll try again soon",
    "never_tried": "⏸️ Holding off on {t}: {why}",
    "enqueue_failed": "⚠️ Borrowed {t} but couldn't queue it for import and return: {why}",
}
INLINE_REASON = {"failed", "never_tried", "enqueue_failed"}
STARTS = {"libby", "import", "calibre"}
REPLY_ONLY = {"metadata", "cover", "media"}
SKIP = {("import", "dismiss")}
SOURCES = {"calibre": "Calibre", "audiobookshelf": "Audiobookshelf"}

LIBBY_ENGINE_URL = os.environ.get("LIBBY_ENGINE_URL", "http://host.docker.internal:5007").rstrip("/")
CARDS_TTL_S = 3600
_cards: dict = {}          # cardId → library name, from Libby's /api/cards/status
_cards_ts = 0.0

_queue: "queue.Queue" = queue.Queue(maxsize=200)
_start_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_threads: Optional[dict] = None     # key → {"root_id", "created", "ts"}; loaded lazily from THREADS_FILE
_channel_id: Optional[str] = None


def _token() -> str:
    return os.environ.get("BOOKWORM_TOKEN", "").strip()


def _safe(s) -> str:
    """Plain text for chat: no @-pings, no stray newlines."""
    return str(s).replace("@", "@​").replace("\n", " ")


def thread_key(title: Optional[str]) -> Optional[str]:
    """Normalise a title so 'Small Town, Big Magic: A Novel' and 'small town big
    magic' share a thread."""
    if not title:
        return None
    t = str(title).lower()
    head = t.split(":")[0]
    t = head if head.strip() else t
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return t or None


_FILE_EXT = re.compile(r"\.(epub|acsm|pdf|mobi|azw3?|kepub)$", re.I)
_FOR_KINDLE = re.compile(r"[\s_-]*for[\s_]+kindle\s*$", re.I)


def display_title(category: str, title: Optional[str]) -> Optional[str]:
    """Calibre watcher titles are often filenames ('The_Fires_of_December_by_Brandon_
    Sanderson_for_Kindle'). For calibre events whose title looks like one, give the plain
    title ('The Fires of December') so the thread key and the chat line match the
    GreatReads link event. Anything else is returned untouched."""
    if category != "calibre" or not title:
        return title
    t = str(title).strip()
    if not ("_" in t or _FILE_EXT.search(t) or _FOR_KINDLE.search(t)):
        return title
    t = _FILE_EXT.sub("", t)
    # Libby's .acsm names end in "_<title id>" ("The Unbound_2349865.acsm"); drop the
    # id or the thread key won't match the clean title (#353).
    t = re.sub(r"_\d{5,}$", "", t).replace("_", " ")
    t = _FOR_KINDLE.sub("", t)
    i = t.lower().rfind(" by ")
    if i > 0:
        t = t[:i]
    t = re.sub(r"\s+", " ", t).strip()
    return t or title


def _card_library(card_id) -> Optional[str]:
    """Library name for a Libby card, cached for an hour. Worker thread only: it
    may hit the engine. None when unknown or the engine is down."""
    global _cards, _cards_ts
    if not card_id:
        return None
    if time.time() - _cards_ts > CARDS_TTL_S or str(card_id) not in _cards:
        try:
            import httpx
            r = httpx.get(f"{LIBBY_ENGINE_URL}/api/cards/status", timeout=5)
            r.raise_for_status()
            _cards = {str(c.get("cardId")): c.get("name") or c.get("cardLabel")
                      for c in (r.json() or {}).get("cards", []) if c.get("cardId")}
        except Exception as exc:   # noqa: BLE001
            logger.debug("bookworm: cards lookup failed: %s", exc)
        _cards_ts = time.time()    # failed or not, don't re-ask on every event
    return _cards.get(str(card_id))


def _format(event: str, d: dict) -> str:
    return "audiobook" if event.startswith("audiobook") or d.get("media") == "audiobook" else "ebook"


def _words(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "an unknown number of"


def render(category: str, event: str, level: str, title: Optional[str],
           detail: Optional[dict], library: Optional[str] = None) -> Optional[str]:
    """The chat line for an event, or None if Bookworm should stay quiet.
    ``library`` overrides ``detail['library']`` (the worker resolves card ids)."""
    if (category, event) in SKIP or category not in STARTS | REPLY_ONLY:
        return None
    d = detail if isinstance(detail, dict) else {}
    t = f"**{_safe(title)}**" if title else "a book"
    if event == "audiobook_download_start" and d.get("borrowed"):
        event = "audiobook_borrow_download_start"
    lib = library or d.get("library")
    tmpl = MESSAGES.get((category, event))
    reason_inline = False
    if (category, event) == ("libby", "wishlist_acquire"):
        st = str(d.get("status") or "")
        reason = str(d.get("reason") or "")
        if st == "acquired":
            if d.get("format") == "audiobook" and d.get("acted"):
                st = "acquired_audiobook"
            elif not d.get("acted"):
                st = "acquired_existing"
        elif st == "on_hold" and not d.get("acted"):
            st = "on_hold_existing"
        tmpl = WISHLIST_ACQUIRE.get(st)
        reason_inline = st in INLINE_REASON or st == "engine_unreachable"
    elif (category, event) == ("calibre", "failed"):
        reason_inline = True   # the template already carries the reason
    if tmpl is None:
        if category not in STARTS:
            return None
        tmpl = _safe(f"{category}/{event}: ").replace("{", "{{").replace("}", "}}") + "{t}"
    fields = d.get("fields")
    text = tmpl.format(
        t=t, fmt=_format(event, d), lib=f" from {_safe(lib)}" if lib else "",
        src=SOURCES.get(str(d.get("source")), "the library"),
        fields=(", ".join(map(str, fields)) if isinstance(fields, (list, tuple))
                else str(fields or "details")),
        from_px=d.get("from_px", "?"), to_px=d.get("to_px", "?"),
        text=f": {_safe(d['text'])[:300]}" if d.get("text") else "",
        n=f" (checked {d['results']} results)" if d.get("results") else "",
        why=_safe(str(d.get("reason") or "unknown reason"))[:140],
        reason=_safe(str(d.get("reason") or "unknown reason"))[:140],
        calibre_id=_safe(d.get("calibre_id") or "?"),
        words=_words(d.get("words")),
        series=f" ({_safe(d['series'])[:80]})" if d.get("series") else "",
    )
    err = d.get("error") or d.get("reason")
    if err and not reason_inline and level in ("warn", "warning", "error"):
        text += f"\n> {_safe(err)[:300]}"
    if level == "error" and not text.startswith(("⚠️", "🛑")):
        text = "❌ " + text
    return text


def _open_thread(key: Optional[str]) -> Optional[dict]:
    """The book's live thread, loading the map from disk on first use."""
    global _threads
    if _threads is None:
        try:
            with open(THREADS_FILE) as f:
                _threads = json.load(f)
        except (OSError, ValueError):
            _threads = {}
    th = _threads.get(key) if key else None
    if th and time.time() - th.get("ts", 0) < THREAD_TTL_S:
        return th
    return None


def _stale(th: dict) -> bool:
    """True when the thread's root post is older than STALE_THREAD_S. Judged by when
    the root was created, never by the last reply (#351). Entries saved before
    ``created`` existed fall back to ``ts`` (their last post: the best we know)."""
    return time.time() - th.get("created", th.get("ts", 0)) >= STALE_THREAD_S


def _remember(key: str, root_id: str) -> None:
    now = time.time()
    old = _threads.get(key) or {}
    # ``created`` is the root's creation time (kept across replies, reset on a new
    # root); ``ts`` is the last post, which only drives the TTL pruning below.
    created = old["created"] if old.get("root_id") == root_id and "created" in old else now
    _threads[key] = {"root_id": root_id, "created": created, "ts": now}
    for k in [k for k, v in _threads.items() if now - v.get("ts", 0) >= THREAD_TTL_S]:
        del _threads[k]
    try:
        tmp = THREADS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_threads, f)
        os.replace(tmp, THREADS_FILE)
    except OSError as exc:
        logger.warning("bookworm: couldn't save threads: %s", exc)


def _post(client, category: str, text: str, key: Optional[str], title: Optional[str]) -> None:
    global _channel_id
    th = _open_thread(key)
    if th and _stale(th):
        # Stale root (#351): replies would hide under a day-old post. Reply-only events
        # are dropped; a starting event opens a new Book Report (replaces the entry).
        if category in REPLY_ONLY:
            return
        th = None
    if category in REPLY_ONLY and not th:
        return
    if not _channel_id:
        r = client.get(f"{MM_URL}/api/v4/teams/name/{MM_TEAM}/channels/name/{MM_CHANNEL}")
        r.raise_for_status()
        _channel_id = r.json()["id"]

    def send(message: str, root_id: Optional[str] = None):
        body = {"channel_id": _channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        return client.post(f"{MM_URL}/api/v4/posts", json=body)

    def open_report() -> str:
        r = send(HEADER.format(t=f"**{_safe(title)}**" if title else "a book"))
        r.raise_for_status()
        return r.json()["id"]

    root_id = th["root_id"] if th else open_report()
    r = send(text, root_id)
    if th and r.status_code in (400, 403, 404):
        # Root post gone (deleted / archived): start a fresh Book Report.
        root_id = open_report()
        r = send(text, root_id)
    r.raise_for_status()
    if key:
        _remember(key, root_id)


def _run() -> None:
    import httpx
    with httpx.Client(timeout=10, headers={"Authorization": f"Bearer {_token()}"}) as client:
        while True:
            category, event, level, title, detail, key = _queue.get()
            try:
                d = detail if isinstance(detail, dict) else {}
                lib = None if d.get("library") else _card_library(d.get("card_id"))
                text = render(category, event, level, title, detail, lib)
                if text:
                    _post(client, category, text, key, title)
            except Exception as exc:   # noqa: BLE001 — chat must never break anything
                logger.warning("bookworm post failed (%s): %s", category, exc)
            finally:
                _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        with _start_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run, daemon=True, name="bookworm")
                _worker.start()


def notify(category: str, event: str, *, level: str = "info", book_id: Optional[int] = None,
           title: Optional[str] = None, detail: Optional[dict] = None) -> None:
    """Hand one event to Bookworm. Returns immediately; never raises."""
    try:
        if not _token():
            return
        title = display_title(category, title)
        if category in REPLY_ONLY and _threads is not None:
            th = _open_thread(thread_key(title))
            if not th or _stale(th):
                return   # cheap early drop for backfills; the worker re-checks anyway
        if not render(category, event, level, title, detail):
            return   # cheap check here; the worker re-renders with the library name
        _ensure_worker()
        _queue.put_nowait((category, event, level, title, detail, thread_key(title)))
    except queue.Full:
        logger.warning("bookworm queue full; dropped %s/%s", category, event)
    except Exception as exc:   # noqa: BLE001
        logger.warning("bookworm notify failed: %s", exc)
