"""In-memory run bookkeeping for the background scheduler jobs (#341).

Every scheduled job is wrapped by ``tracked(job_id, fn)`` so a stall is visible:
when it last started/ended, how long it took, what it returned, and the last error.
Served by ``GET /api/scheduler/status`` and folded into the Settings backfill card.
Process-local on purpose: it describes THIS process's scheduler, and resets on restart.
"""

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_lock = threading.Lock()
JOB_STATUS: dict = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tracked(job_id: str, fn, quiet: bool = False):
    """Wrap a job: record start/end/duration/result, log it, and on an exception log
    the traceback + a ``scheduler/job_failed`` event instead of dying silently.
    ``quiet`` jobs (every couple of minutes) log their runs at DEBUG; failures always
    log at ERROR."""
    def run():
        with _lock:
            st = JOB_STATUS.setdefault(job_id, {"runs": 0, "failures": 0})
            st.update(running=True, last_start=_now())
        t0 = time.monotonic()
        ok, result, error = True, None, None
        try:
            result = fn()
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
            logger.exception("Scheduled job %s failed", job_id)
            try:
                from .event_log_service import log_event
                log_event("scheduler", "job_failed", level="error",
                          detail={"job": job_id, "error": error})
            except Exception:
                pass
        secs = round(time.monotonic() - t0, 1)
        with _lock:
            st = JOB_STATUS[job_id]
            st.update(running=False, last_end=_now(), last_seconds=secs, last_ok=ok,
                      last_error=error, last_result=result if ok else st.get("last_result"))
            st["runs"] += 1
            if not ok:
                st["failures"] += 1
        if ok:
            (logger.debug if quiet else logger.info)(
                "Scheduled job %s done in %.1fs: %s", job_id, secs, result)
    run.__name__ = job_id
    return run


def snapshot(scheduler=None) -> dict:
    """Every registered job with its next run time, merged with its run bookkeeping."""
    with _lock:
        status = {k: dict(v) for k, v in JOB_STATUS.items()}
    out = {}
    jobs = scheduler.get_jobs() if scheduler is not None else []
    for job in jobs:
        nrt = job.next_run_time
        out[job.id] = {"next_run": nrt.isoformat() if nrt else None, **status.pop(job.id, {})}
    out.update(status)   # tracked runs whose job is no longer registered
    return {"running": bool(scheduler is not None and scheduler.running), "jobs": out}
