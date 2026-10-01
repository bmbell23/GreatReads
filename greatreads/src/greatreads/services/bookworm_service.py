"""Bookworm: post book automation to #greatreads in Mattermost (#320).

Every GreatReads book action already goes through ``log_event`` (#184), so that is
the one hook: ``log_event`` hands each event to :func:`notify`, which filters it,
words it, and queues it for the **bookworm** bot. One thread per book (keyed by
normalised title), so a book's journey reads top to bottom: borrow → .acsm →
Calibre/ABS link → metadata → cover. The thread's root is a fixed "Book Report"
header and every event, the first included, is a reply under it (#323).

Rules:
- **Starts a thread:** Libby events and import links (something new happened).
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
    ("import", "linked"): "🔗 {t} from {src} linked into GreatReads",
    ("import", "created"): "📚 {t} from {src} added to GreatReads",
    ("metadata", "enriched"): "🏷️ Metadata filled in for {t}: {fields}",
    ("cover", "upgraded"): "🖼️ Better cover for {t} ({from_px}px → {to_px}px)",
    # MediaForge, via POST /api/media-events (#326)
    ("media", "chapters_fixed"): "🧩 Chapters fixed for {t}{text}",
    ("media", "chapters_skipped"): "👌 Chapters fine as-is for {t}{text}",
    ("media", "chapters_failed"): "⚠️ Chapter fix failed for {t}{text}",
    ("media", "sync_map_built"): "🔄 Sync map ready for {t}{text}",
    ("media", "sync_map_failed"): "⚠️ Sync map failed for {t}{text}",
}
STARTS = {"libby", "import"}
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
_threads: Optional[dict] = None     # key → {"root_id", "ts"}; loaded lazily from THREADS_FILE
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
    )
    err = d.get("error") or d.get("reason")
    if err and level in ("warn", "warning", "error"):
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


def _remember(key: str, root_id: str) -> None:
    now = time.time()
    _threads[key] = {"root_id": root_id, "ts": now}
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
        if category in REPLY_ONLY and _threads is not None and not _open_thread(thread_key(title)):
            return   # cheap early drop for backfills; the worker re-checks anyway
        if not render(category, event, level, title, detail):
            return   # cheap check here; the worker re-renders with the library name
        _ensure_worker()
        _queue.put_nowait((category, event, level, title, detail, thread_key(title)))
    except queue.Full:
        logger.warning("bookworm queue full; dropped %s/%s", category, event)
    except Exception as exc:   # noqa: BLE001
        logger.warning("bookworm notify failed: %s", exc)
