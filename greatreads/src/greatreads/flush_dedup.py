"""#370: idempotency for outbox replays.

The phone queues progress writes while the server is unreachable and replays
them later. A replay can arrive twice (the first attempt was applied but the
response was lost). Each queued request carries a ``flush_id``; the handler
checks ``already()`` before applying and calls ``remember()`` only after a
successful apply (so a failed attempt can still be retried).

In-memory LRU only (no schema change): a server restart forgets ids, which at
worst re-applies one duplicate delta, the same exposure as before this feature.
"""
import threading
from collections import OrderedDict

_MAX = 20000
_seen = OrderedDict()
_lock = threading.Lock()


def already(fid) -> bool:
    if not fid or not isinstance(fid, str):
        return False
    with _lock:
        if fid in _seen:
            _seen.move_to_end(fid)
            return True
        return False


def remember(fid) -> None:
    if not fid or not isinstance(fid, str):
        return
    with _lock:
        _seen[fid] = True
        _seen.move_to_end(fid)
        while len(_seen) > _MAX:
            _seen.popitem(last=False)
