"""Reading session management API routes."""

from datetime import date, datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_

from ..database import get_db
from ..models.reading import Reading, ReadingCreate, ReadingUpdate, ReadingResponse
from ..services.chain_calculator import ChainCalculator
from ._book_enrich import enrich_book_dict

router = APIRouter()


def normalize_media_type(media: Optional[str]) -> Optional[str]:
    """Normalize legacy media types to standard values."""
    if not media:
        return media

    normalized = {
        'hardcover': 'Physical',
        'audiobook': 'Audio'
    }

    return normalized.get(media.lower(), media)


def _reading_dict_with_enriched_book(reading: Reading, db: Session) -> dict:
    """Serialize a reading and enrich its embedded book with source/inventory
    fields (calibre_id, abs_id, inventory, media_owned) for the shared popup."""
    data = reading.to_dict()
    if data.get("book") and reading.book_id is not None:
        enrich_book_dict(data["book"], reading.book_id, db)
    return data


@router.get("/")
async def get_readings(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    status: Optional[str] = Query(None),
    media: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Get readings with optional filtering."""
    query = db.query(Reading).options(
        joinedload(Reading.book)
    )

    if status:
        if status == "finished":
            query = query.filter(Reading.date_finished_actual.isnot(None))
        elif status == "in_progress":
            query = query.filter(
                and_(
                    Reading.date_started.isnot(None),
                    Reading.date_finished_actual.is_(None),
                    Reading.date_paused.is_(None)  # Exclude paused books
                )
            )
        elif status == "not_started":
            query = query.filter(Reading.date_started.is_(None))

    if media:
        query = query.filter(Reading.media.ilike(f"%{media}%"))

    readings = query.offset(skip).limit(limit).all()
    # In-progress readings power the Home page, which wants owned-format icons —
    # enrich those with inventory/source fields (superset of to_dict; #65).
    if status == "in_progress":
        return [_reading_dict_with_enriched_book(r, db) for r in readings]
    return [reading.to_dict() for reading in readings]


@router.get("/tbr")
async def get_tbr_readings(db: Session = Depends(get_db)):
    """Get TBR (To Be Read) readings in chain order."""
    # Get TBR readings: not-started OR paused (active in-progress books live on the
    # Home page now, #65). Active in-progress = started AND not paused AND not finished.
    readings = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(
        Reading.date_finished_actual.is_(None),
        or_(
            Reading.date_started.is_(None),      # not started
            Reading.date_paused.isnot(None),     # or paused
        )
    ).all()

    # Sort by chain order
    # IP books (date_started is not None) should always come before NS books
    # Within each group, sort by date (date_started for IP, date_est_start for NS)
    readings.sort(key=lambda r: (
        0 if r.date_started else 1,  # IP books (0) before NS books (1)
        r.date_started or r.date_est_start or date(2099, 1, 1),  # Then by date
        r.id  # Then by ID for stable sort
    ))

    # Convert to dict, enriching each book with source links + owned media so the
    # shared cover-tap popup can offer Read/Listen and show shelf location.
    return [_reading_dict_with_enriched_book(r, db) for r in readings]


@router.get("/journal")
async def get_journal_readings(db: Session = Depends(get_db)):
    """Get finished readings sorted by date finished (most recent first)."""
    # Get all finished readings with book data
    readings = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(
        Reading.date_finished_actual.isnot(None)
    ).order_by(
        Reading.date_finished_actual.desc()
    ).all()

    # Convert to dict, enriching each book with source links + owned media so the
    # shared cover-tap popup can offer Read/Listen and show shelf location.
    return [_reading_dict_with_enriched_book(r, db) for r in readings]


@router.get("/{reading_id}")
async def get_reading(reading_id: int, db: Session = Depends(get_db)):
    """Get a specific reading."""
    reading = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(Reading.id == reading_id).first()
    if not reading:
        raise HTTPException(status_code=404, detail="Reading not found")
    return reading.to_dict()


@router.post("/")
async def create_reading(reading: ReadingCreate, db: Session = Depends(get_db)):
    """Create a new reading."""
    reading_data = reading.dict()
    # Normalize media type
    if reading_data.get('media'):
        reading_data['media'] = normalize_media_type(reading_data['media'])

    # Find the last unfinished reading of the same media type to link to
    media = reading_data.get('media')
    if media:
        # Get all unfinished readings of the same media type
        same_media_readings = db.query(Reading).filter(
            and_(
                Reading.media == media,
                Reading.date_finished_actual.is_(None)
            )
        ).all()

        if same_media_readings:
            # Sort by date_est_start to find the last one in the chain
            same_media_readings.sort(key=lambda r: (
                r.date_est_start or r.date_started or date(2099, 1, 1),
                r.id
            ))

            # Find the last reading in the chain (one that no other reading points to)
            reading_ids = {r.id for r in same_media_readings}
            last_reading = None

            for r in same_media_readings:
                # Check if any other reading points to this one
                has_next = any(other.id_previous == r.id for other in same_media_readings)
                if not has_next:
                    last_reading = r
                    break

            # If we found a last reading, link to it
            if last_reading:
                reading_data['id_previous'] = last_reading.id

    db_reading = Reading(**reading_data)
    db.add(db_reading)
    db.commit()
    db.refresh(db_reading)

    # Recalculate chains after creating new reading
    calculator = ChainCalculator(db)
    calculator.recalculate_all_chains()

    # Reload with book data
    db.refresh(db_reading)
    db_reading = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(Reading.id == db_reading.id).first()

    return db_reading.to_dict()


@router.put("/{reading_id}")
async def update_reading(reading_id: int, reading: ReadingUpdate, db: Session = Depends(get_db)):
    """Update a reading."""
    from datetime import date
    import math

    db_reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not db_reading:
        raise HTTPException(status_code=404, detail="Reading not found")

    # Store old media type before updating
    old_media = db_reading.media

    update_data = reading.dict(exclude_unset=True)
    # Normalize media type if present
    if 'media' in update_data and update_data['media']:
        update_data['media'] = normalize_media_type(update_data['media'])

    for field, value in update_data.items():
        setattr(db_reading, field, value)

    # If current_percent was updated, recalculate days_estimate
    if 'current_percent' in update_data and db_reading.current_percent is not None:
        # Reload with book data for calculation
        db.refresh(db_reading)
        db_reading = db.query(Reading).options(joinedload(Reading.book)).filter(Reading.id == reading_id).first()

        if db_reading.date_started and db_reading.book and db_reading.current_percent > 0 and db_reading.current_percent < 100:
            today = date.today()
            days_elapsed = (today - db_reading.date_started).days + 1

            from ..services.settings_service import get_wpd_for_media
            wpd = get_wpd_for_media(db, db_reading.media)

            total_words = db_reading.book.word_count
            words_read = total_words * (db_reading.current_percent / 100.0)
            words_remaining = total_words - words_read

            days_remaining = math.ceil(words_remaining / wpd)
            new_days_estimate = days_elapsed + days_remaining

            db_reading.days_estimate = new_days_estimate
            db_reading.days_estimate_override = True
            db_reading.current_percent_manual_override = True
            db_reading.date_progress_set = datetime.now()

    db.commit()
    db.refresh(db_reading)

    # Check if media type changed
    calculator = ChainCalculator(db)
    if 'media' in update_data and old_media != db_reading.media:
        # Handle format change - this will rebuild chains and recalculate
        calculator.handle_format_change(reading_id, old_media, db_reading.media)
    else:
        # Just recalculate chains
        calculator.recalculate_all_chains()

    # Reload with book data
    db_reading = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(Reading.id == reading_id).first()

    return db_reading.to_dict()


@router.put("/{reading_id}/progress")
async def update_reading_progress(
    reading_id: int,
    current_percent: float,
    minutes_read: Optional[float] = None,
    session_id: Optional[str] = None,
    session_start_ms: Optional[int] = None,
    session_seconds: Optional[float] = None,
    start_percent: Optional[float] = None,
    db: Session = Depends(get_db)
):
    """Realign the current progress percentage for an in-progress reading.

    This will:
    1. Accept that the user is at current_percent right now
    2. Adjust date_started backwards to align with this progress
    3. Continue auto-calculating from this point forward
    """
    from datetime import date, timedelta

    db_reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not db_reading:
        raise HTTPException(status_code=404, detail="Reading not found")

    if not db_reading.is_started or db_reading.is_finished:
        raise HTTPException(status_code=400, detail="Can only update progress for in-progress readings")

    # Validate percentage
    if current_percent < 0 or current_percent > 100:
        raise HTTPException(status_code=400, detail="Percentage must be between 0 and 100")

    # Calculate new days_estimate based on actual progress and WPD
    if db_reading.date_started and db_reading.book and current_percent > 0 and current_percent < 100:
        import math
        today = date.today()
        days_elapsed = (today - db_reading.date_started).days + 1  # +1 for inclusive

        # Get the WPD for this media type
        from ..services.settings_service import get_wpd_for_media
        wpd = get_wpd_for_media(db, db_reading.media)

        # Calculate words read and remaining based on percentage
        total_words = db_reading.book.word_count
        words_read = total_words * (current_percent / 100.0)
        words_remaining = total_words - words_read

        # Calculate days remaining based on WPD (round up)
        days_remaining = math.ceil(words_remaining / wpd)

        # Total days = elapsed + remaining
        new_days_estimate = days_elapsed + days_remaining

        # Update days_estimate and mark it as overridden
        db_reading.days_estimate = new_days_estimate
        db_reading.days_estimate_override = True

    # Save the manual percent override and the datetime it was set
    db_reading.current_percent = current_percent
    db_reading.current_percent_manual_override = True
    db_reading.date_progress_set = datetime.now()

    # Physical books log no reading time, so derive their daily "words read" from
    # the page/percent delta and record it to reading_activity so they appear on
    # the stats words/day chart + goal calendar (#39). A day's physical words =
    # (today's end position − previous logged days' total) × word_count: we
    # overwrite today's row so multiple updates/day collapse to the latest, and
    # words land only on days the user actually logs. (key per-reading to keep
    # rereads / the ebook edition's own activity separate.)
    media_lower = (db_reading.media or "").lower()
    if media_lower in ("physical", "hardcover", "paperback") and db_reading.book and db_reading.book.word_count:
        from sqlalchemy import text as _sql
        today_str = date.today().isoformat()
        book_key = f"phys:{db_reading.id}"
        word_count = db_reading.book.word_count
        target_total = (current_percent / 100.0) * word_count
        db.execute(_sql(
            "CREATE TABLE IF NOT EXISTS reading_activity ("
            " activity_date TEXT NOT NULL, book_key TEXT NOT NULL, format TEXT NOT NULL,"
            " minutes REAL NOT NULL DEFAULT 0, words INTEGER NOT NULL DEFAULT 0,"
            " wpm_mpw_sum REAL NOT NULL DEFAULT 0, wpm_n INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY(activity_date, book_key, format))"))
        logged_before = db.execute(_sql(
            "SELECT COALESCE(SUM(words),0) FROM reading_activity "
            "WHERE book_key=:bk AND format='Physical' AND activity_date < :d"),
            {"bk": book_key, "d": today_str}).scalar() or 0
        today_words = max(0, round(target_total - logged_before))
        # Words always OVERWRITE today's row (position-based delta from prior days).
        # Minutes only overwrite when the caller actually carries a value: a page-only
        # progress save (minutes_read=None) must NOT clobber today's logged minutes to
        # 0. minutes_read, when given, is the *today total* the UI carries (loaded via
        # /today-minutes, edited up, re-sent). Naturally resets at midnight (new date =
        # new row). (#39/#40, minutes-preservation fix #44)
        if minutes_read is None:
            db.execute(_sql(
                "INSERT INTO reading_activity(activity_date,book_key,format,minutes,words,wpm_mpw_sum,wpm_n) "
                "VALUES(:d,:bk,'Physical',0,:w,0,0) "
                "ON CONFLICT(activity_date,book_key,format) DO UPDATE SET "
                "words=excluded.words"),
                {"d": today_str, "bk": book_key, "w": today_words})
        else:
            db.execute(_sql(
                "INSERT INTO reading_activity(activity_date,book_key,format,minutes,words,wpm_mpw_sum,wpm_n) "
                "VALUES(:d,:bk,'Physical',:mins,:w,0,0) "
                "ON CONFLICT(activity_date,book_key,format) DO UPDATE SET "
                "words=excluded.words, minutes=excluded.minutes"),
                {"d": today_str, "bk": book_key, "w": today_words, "mins": max(0.0, float(minutes_read))})

        # Record this sitting as a reading_sessions row too (#78), so physical
        # sittings appear in the per-session view like ebook/audio. PURELY ADDITIVE:
        # the daily reading_activity above stays the stats source — only ebook rows
        # are ever rederived from sessions, so physical sessions are never rolled up
        # and cannot double-count. minutes come from the session timer; words are
        # THIS session's position delta (end% − start%), not the daily cumulative.
        if session_id and session_start_ms:
            now_ms = int(datetime.now().timestamp() * 1000)
            sess_minutes = round(max(0.0, float(session_seconds or 0)) / 60.0, 2)
            start_pct_val = max(0.0, min(100.0,
                float(start_percent if start_percent is not None else current_percent)))
            sess_words = max(0, round((current_percent - start_pct_val) / 100.0 * word_count))
            db.execute(_sql(
                "CREATE TABLE IF NOT EXISTS reading_sessions ("
                " id TEXT PRIMARY KEY, book_key TEXT NOT NULL, format TEXT NOT NULL,"
                " started_at INTEGER NOT NULL, ended_at INTEGER, activity_date TEXT,"
                " minutes REAL NOT NULL DEFAULT 0, words INTEGER NOT NULL DEFAULT 0,"
                " start_pct REAL, end_pct REAL, wpm_mpw_sum REAL NOT NULL DEFAULT 0,"
                " wpm_n INTEGER NOT NULL DEFAULT 0, device TEXT, updated INTEGER)"))
            db.execute(_sql(
                "INSERT INTO reading_sessions(id,book_key,format,started_at,ended_at,"
                " activity_date,minutes,words,start_pct,end_pct,wpm_mpw_sum,wpm_n,device,updated) "
                "VALUES(:id,:bk,'Physical',:st,:en,:d,:mins,:w,:sp,:ep,0,0,'physical',:u) "
                "ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at, minutes=excluded.minutes,"
                " words=excluded.words, end_pct=excluded.end_pct, updated=excluded.updated"),
                {"id": str(session_id), "bk": book_key, "st": int(session_start_ms),
                 "en": now_ms, "d": today_str, "mins": sess_minutes, "w": sess_words,
                 "sp": start_pct_val / 100.0, "ep": current_percent / 100.0, "u": now_ms})

    db.commit()

    # Mirror this % into the cross-format progress store (ereader_progress) so
    # opening the ebook / audiobook resumes here — e.g. physical reading drives the
    # ebook's resume point. Keyed by the unified key (calibre id, else abs:<id>);
    # percent-based, no chapter anchor. Best-effort; never blocks the save. (#42)
    try:
        from sqlalchemy import text as _sql
        import time as _t, json as _j
        ext = db.execute(_sql(
            "SELECT source, external_id FROM external_imports WHERE book_id=:b"),
            {"b": db_reading.book_id}).fetchall()
        cal = next((e[1] for e in ext if e[0] == 'calibre'), None)
        abs_id = next((e[1] for e in ext if e[0] == 'audiobookshelf'), None)
        key = str(cal) if cal else (('abs:' + str(abs_id)) if abs_id else None)
        if key:
            media_l = (db_reading.media or '').lower()
            mt = 'audiobook' if media_l in ('audio', 'audiobook') else (
                'physical' if media_l in ('physical', 'hardcover', 'paperback') else 'ebook')
            frac = max(0.0, min(1.0, current_percent / 100.0))
            now_ms = int(_t.time() * 1000)
            rec = _j.dumps({'progress': frac, 'updated': now_ms, 'mediaType': mt, 'source': 'greatreads'})
            db.execute(_sql(
                "CREATE TABLE IF NOT EXISTS ereader_progress ("
                " book_key TEXT PRIMARY KEY, data TEXT NOT NULL, progress REAL, updated INTEGER)"))
            db.execute(_sql(
                "INSERT INTO ereader_progress(book_key,data,progress,updated) "
                "VALUES(:k,:d,:p,:u) ON CONFLICT(book_key) DO UPDATE SET "
                "data=excluded.data, progress=excluded.progress, updated=excluded.updated"),
                {"k": key, "d": rec, "p": frac, "u": now_ms})
            db.commit()
    except Exception as _e:
        db.rollback()
        print(f"cross-format progress mirror failed: {_e}")

    # Recalculate chains to update estimated end date
    calculator = ChainCalculator(db)
    calculator.recalculate_all_chains()

    # Reload with book data
    db_reading = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(Reading.id == reading_id).first()

    return db_reading.to_dict()


@router.get("/{reading_id}/today-minutes")
async def get_reading_today_minutes(reading_id: int, db: Session = Depends(get_db)):
    """Today's logged physical reading minutes for this reading (#40). The popup
    loads this so 'Time read today' shows the running total it can edit up."""
    from sqlalchemy import text as _sql
    from datetime import date as _date
    try:
        m = db.execute(_sql(
            "SELECT COALESCE(SUM(minutes),0) FROM reading_activity "
            "WHERE book_key=:bk AND format='Physical' AND activity_date=:d"),
            {"bk": f"phys:{reading_id}", "d": _date.today().isoformat()}).scalar() or 0
    except Exception:
        m = 0
    return {"minutes": round(float(m))}


@router.delete("/{reading_id}")
async def delete_reading(reading_id: int, db: Session = Depends(get_db)):
    """Delete a reading while maintaining chain integrity."""
    db_reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not db_reading:
        raise HTTPException(status_code=404, detail="Reading not found")

    # CRITICAL: Update chain links BEFORE deleting to maintain chain integrity
    # Find all readings that point to this one
    readings_pointing_here = db.query(Reading).filter(Reading.id_previous == reading_id).all()

    # Update them to point to what this reading was pointing to
    for reading in readings_pointing_here:
        reading.id_previous = db_reading.id_previous

    # Commit the chain link updates before deletion
    db.commit()

    # Now safe to delete the reading
    db.delete(db_reading)
    db.commit()

    # Recalculate chains after deleting reading
    calculator = ChainCalculator(db)
    calculator.recalculate_all_chains()

    return {"message": "Reading deleted successfully"}


@router.post("/{reading_id}/finish")
async def finish_reading(reading_id: int, db: Session = Depends(get_db)):
    """Mark a reading as finished with today's date."""
    from datetime import date

    db_reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not db_reading:
        raise HTTPException(status_code=404, detail="Reading not found")

    if db_reading.date_finished_actual:
        raise HTTPException(status_code=400, detail="Reading is already finished")

    db_reading.date_finished_actual = date.today()

    # If not started, set start date to today as well
    if not db_reading.date_started:
        db_reading.date_started = date.today()

    db.commit()
    db.refresh(db_reading)

    # Recalculate chains and start next book
    calculator = ChainCalculator(db)
    calculator.finish_reading_and_start_next(reading_id)

    # Reload with book data
    db_reading = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(Reading.id == reading_id).first()

    return db_reading.to_dict()


@router.post("/{reading_id}/pause")
async def pause_reading(reading_id: int, db: Session = Depends(get_db)):
    """Pause a reading - freezes progress at current point."""
    from datetime import date

    db_reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not db_reading:
        raise HTTPException(status_code=404, detail="Reading not found")

    if not db_reading.is_started or db_reading.is_finished:
        raise HTTPException(status_code=400, detail="Can only pause in-progress readings")

    if db_reading.is_paused:
        raise HTTPException(status_code=400, detail="Reading is already paused")

    # Set pause date and freeze current progress
    db_reading.date_paused = date.today()

    # Store current progress if not already set
    if db_reading.current_percent is None:
        db_reading.current_percent = db_reading.current_progress_percent or 0.0

    db.commit()

    # Reload with book data
    db_reading = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(Reading.id == reading_id).first()

    return db_reading.to_dict()


@router.post("/{reading_id}/unpause")
async def unpause_reading(reading_id: int, db: Session = Depends(get_db)):
    """Unpause a reading - resumes progress calculation."""
    from datetime import date, timedelta

    db_reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not db_reading:
        raise HTTPException(status_code=404, detail="Reading not found")

    if not db_reading.is_paused:
        raise HTTPException(status_code=400, detail="Reading is not paused")

    # Calculate how long it was paused
    days_paused = (date.today() - db_reading.date_paused).days

    # Adjust start date forward by the paused duration
    # This makes it as if the reading started later
    db_reading.date_started = db_reading.date_started + timedelta(days=days_paused)

    # Clear pause date
    db_reading.date_paused = None

    db.commit()

    # Recalculate chains
    calculator = ChainCalculator(db)
    calculator.recalculate_all_chains()

    # Reload with book data
    db_reading = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(Reading.id == reading_id).first()

    return db_reading.to_dict()


@router.post("/{reading_id}/start")
async def start_reading(
    reading_id: int,
    start_date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Manually start a reading with an optional start date."""
    from datetime import date, datetime

    db_reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not db_reading:
        raise HTTPException(status_code=404, detail="Reading not found")

    if db_reading.is_started:
        raise HTTPException(status_code=400, detail="Reading is already started")

    # Parse start date or use today
    if start_date:
        try:
            parsed_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        parsed_date = date.today()

    db_reading.date_started = parsed_date

    db.commit()

    # Recalculate chains
    calculator = ChainCalculator(db)
    calculator.recalculate_all_chains()

    # Reload with book data
    db_reading = db.query(Reading).options(
        joinedload(Reading.book)
    ).filter(Reading.id == reading_id).first()

    return db_reading.to_dict()


@router.post("/reorder")
async def reorder_readings(
    reading_id: int,
    new_position: int,
    db: Session = Depends(get_db)
):
    """Reorder a reading in the chain."""
    reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not reading:
        raise HTTPException(status_code=404, detail="Reading not found")

    calculator = ChainCalculator(db)
    calculator.reorder_reading(reading_id, new_position)

    return {"message": "Reading reordered successfully"}


@router.post("/bulk-reorder")
async def bulk_reorder_readings(
    reading_ids: List[int],
    db: Session = Depends(get_db)
):
    """Bulk reorder readings based on a new order of IDs.

    The reading_ids list should contain all unfinished reading IDs in the desired order.
    This will rebuild the chains and recalculate dates.
    """
    # Get all the readings
    readings_dict = {r.id: r for r in db.query(Reading).filter(Reading.id.in_(reading_ids)).all()}

    # Verify all IDs exist
    if len(readings_dict) != len(reading_ids):
        raise HTTPException(status_code=404, detail="Some readings not found")

    # Create ordered list
    ordered_readings = [readings_dict[rid] for rid in reading_ids]

    # Rebuild chains from this new order
    calculator = ChainCalculator(db)
    calculator._rebuild_chains_from_order(ordered_readings)
    calculator.recalculate_all_chains()

    return {"message": "Readings reordered successfully"}


class BulkFormatUpdate(BaseModel):
    reading_ids: List[int]
    new_format: str


@router.post("/bulk-update-format")
async def bulk_update_format(payload: BulkFormatUpdate, db: Session = Depends(get_db)):
    """Bulk change the format/media of multiple readings.

    Moves each reading into the chain for the new format, preserving its
    relative position (by estimated start date) within the target format.
    """
    new_format = normalize_media_type(payload.new_format)
    valid_formats = {'Ebook', 'Audio', 'Physical'}
    if new_format not in valid_formats:
        raise HTTPException(status_code=400, detail=f"Invalid format: {payload.new_format}")

    readings = db.query(Reading).filter(Reading.id.in_(payload.reading_ids)).all()
    if len(readings) != len(set(payload.reading_ids)):
        raise HTTPException(status_code=404, detail="Some readings not found")

    calculator = ChainCalculator(db)
    changed = 0
    for reading in readings:
        old_media = reading.media
        if old_media == new_format:
            continue
        reading.media = new_format
        db.commit()
        # Move the reading into the new format's chain (handles IP/NS positioning,
        # chain relinking, and WPD-based estimate recalculation).
        calculator.handle_format_change(reading.id, old_media, new_format)
        changed += 1

    return {"message": f"Updated format for {changed} readings", "changed": changed}
