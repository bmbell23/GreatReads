"""Bookworm: post book automation to #greatreads in Mattermost (#320).

Every GreatReads book action already goes through ``log_event`` (#184), so that is
the one hook: ``log_event`` hands each event to :func:`notify`, which filters it,
words it, and queues it for the **bookworm** bot. One thread per book (keyed by
normalised title), so a book's journey reads top to bottom: borrow → .acsm →
Calibre/ABS link → metadata → cover.

Rules:
- **Starts a thread:** Libby events and import links (something new happened).
- **Reply-only:** metadata enrichment and cover upgrades. They only post into a
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

# (category, event) → message. ``{t}`` is the bolded title. Unlisted events get a
# generic line in "starts" categories and are dropped otherwise.
MESSAGES = {
    ("libby", "auto_borrow"): "🤖 Hold was ready, so I borrowed {t} automatically",
    ("libby", "borrow_start"): "📥 Borrowing {t} from Libby…",
    ("libby", "borrow"): "📥 Borrowed {t} from Libby",
    ("libby", "borrow_failed"): "⚠️ Couldn't borrow {t} from Libby",
    ("libby", "hold_claimed"): "🎟️ Claimed the ready hold on {t}",
    ("libby", "acsm_download"): "📄 Got the .acsm for {t}; the Calibre watcher takes it from here",
    ("libby", "download_failed"): "⚠️ Borrowed {t} but the .acsm download failed",
    ("libby", "return"): "↩️ Returned {t} to Libby (import confirmed)",
    ("libby", "autofulfill_parked"): "⏸️ Auto-fulfill parked {t} after repeated failures",
    ("libby", "autofulfill_giveup"): "🛑 Gave up confirming the import of {t}; the loan is kept",
    ("libby", "audiobook_download_start"): "🎧 Downloading the audiobook of {t}…",
    ("libby", "audiobook_download"): "🎧 Audiobook of {t} downloaded",
    ("libby", "audiobook_download_failed"): "⚠️ Audiobook download failed for {t}",
    ("libby", "audiobook_borrow_manual"): "✋ Borrowed the audiobook of {t}; it needs a manual harvest",
    ("import", "linked"): "🔗 {t} from {src} linked into GreatReads",
    ("import", "created"): "📚 {t} from {src} added to GreatReads",
    ("metadata", "enriched"): "🏷️ Metadata filled in for {t}: {fields}",
    ("cover", "upgraded"): "🖼️ Better cover for {t} ({from_px}px → {to_px}px)",
}
STARTS = {"libby", "import"}
REPLY_ONLY = {"metadata", "cover"}
SKIP = {("import", "dismiss")}
SOURCES = {"calibre": "Calibre", "audiobookshelf": "Audiobookshelf"}

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


def render(category: str, event: str, level: str, title: Optional[str],
           detail: Optional[dict]) -> Optional[str]:
    """The chat line for an event, or None if Bookworm should stay quiet."""
    if (category, event) in SKIP or category not in STARTS | REPLY_ONLY:
        return None
    d = detail if isinstance(detail, dict) else {}
    t = f"**{_safe(title)}**" if title else "a book"
    tmpl = MESSAGES.get((category, event))
    if tmpl is None:
        if category not in STARTS:
            return None
        tmpl = _safe(f"{category}/{event}: ").replace("{", "{{").replace("}", "}}") + "{t}"
    fields = d.get("fields")
    text = tmpl.format(
        t=t,
        src=SOURCES.get(str(d.get("source")), "the library"),
        fields=(", ".join(map(str, fields)) if isinstance(fields, (list, tuple))
                else str(fields or "details")),
        from_px=d.get("from_px", "?"), to_px=d.get("to_px", "?"),
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


def _post(client, category: str, text: str, key: Optional[str]) -> None:
    global _channel_id
    th = _open_thread(key)
    if category in REPLY_ONLY and not th:
        return
    if not _channel_id:
        r = client.get(f"{MM_URL}/api/v4/teams/name/{MM_TEAM}/channels/name/{MM_CHANNEL}")
        r.raise_for_status()
        _channel_id = r.json()["id"]
    body = {"channel_id": _channel_id, "message": text}
    if th:
        body["root_id"] = th["root_id"]
    r = client.post(f"{MM_URL}/api/v4/posts", json=body)
    if th and r.status_code in (400, 403, 404):
        # Root post gone (deleted / archived): start a fresh thread for the book.
        body.pop("root_id")
        th = None
        r = client.post(f"{MM_URL}/api/v4/posts", json=body)
    r.raise_for_status()
    if key:
        _remember(key, th["root_id"] if th else r.json()["id"])


def _run() -> None:
    import httpx
    with httpx.Client(timeout=10, headers={"Authorization": f"Bearer {_token()}"}) as client:
        while True:
            category, text, key = _queue.get()
            try:
                _post(client, category, text, key)
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
        text = render(category, event, level, title, detail)
        if not text:
            return
        _ensure_worker()
        _queue.put_nowait((category, text, thread_key(title)))
    except queue.Full:
        logger.warning("bookworm queue full; dropped %s/%s", category, event)
    except Exception as exc:   # noqa: BLE001
        logger.warning("bookworm notify failed: %s", exc)
