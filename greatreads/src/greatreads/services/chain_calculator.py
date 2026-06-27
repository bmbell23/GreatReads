"""Chain calculation service - ports the logic from the CLI update-readings command."""

import math
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from ..models.reading import Reading
from .settings_service import get_reading_speeds
from .format_dominance import get_primary_format


class ChainCalculator:
    """Service for calculating and updating reading chains."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def recalculate_all_chains(self):
        """Recalculate all reading chains - equivalent to update-readings --all."""
        unfinished_readings = self.db.query(Reading).filter(
            Reading.date_finished_actual.is_(None)
        ).all()

        reading_speeds = get_reading_speeds(self.db)

        # Active in-progress books live on Home (#65), NOT in the TBR chains. They get
        # standalone dates (start = their real start) and ANCHOR the per-format chains.
        # They must never sit inside a not-started chain — a stale/branched link there
        # (e.g. from an old reorder) would fork the walk and corrupt the order. The TBR
        # chains are formed only by not-started + paused books.
        active_ip = [r for r in unfinished_readings
                     if r.date_started is not None and r.date_paused is None]
        chain_readings = [r for r in unfinished_readings
                          if not (r.date_started is not None and r.date_paused is None)]

        # Per-format anchors (#67 phase 3): each format's chain starts the day after the
        # active in-progress book that is PRIMARY in that format finishes (computed from
        # cumulative word-equivalents), so a book read mostly in another format dictates
        # *that* format's chain; a format with no such book rolls to tomorrow.
        primary_anchors = self._compute_primary_anchors(active_ip, reading_speeds)

        # 1) Active IP books: standalone dates; detach from any chain (repairs old links).
        for r in active_ip:
            self._apply_started_dates(r, reading_speeds)
            r.id_previous = None

        # 2) Not-started + paused books form the per-format TBR chains.
        chain_map = self._build_chain_map(chain_readings)
        chain_heads = self._find_chain_heads(chain_readings)
        for head in chain_heads:
            self._recalculate_chain_from_head(head, chain_map, primary_anchors)

        self.db.commit()
    
    def _build_chain_map(self, readings: List[Reading]) -> Dict[int, Reading]:
        """Build a map of reading ID to reading object."""
        return {reading.id: reading for reading in readings}
    
    def _find_chain_heads(self, readings: List[Reading]) -> List[Reading]:
        """Find the head of each chain."""
        chain_heads = []
        reading_ids = {r.id for r in readings}
        
        for reading in readings:
            # A reading is a chain head if:
            # 1. It has no previous reading, OR
            # 2. Its previous reading is not in the unfinished list (i.e., it's finished)
            if not reading.id_previous or reading.id_previous not in reading_ids:
                chain_heads.append(reading)
        
        return chain_heads
    
    def _recalculate_chain_from_head(self, head: Reading, chain_map: Dict[int, Reading],
                                     primary_anchors: Optional[Dict[str, date]] = None):
        """Recalculate dates for a chain starting from the head."""
        current = head
        current_date = self._get_chain_start_date(current)
        primary_anchors = primary_anchors or {}

        # Get reading speeds from settings
        reading_speeds = get_reading_speeds(self.db)

        seen_ns_book = False

        while current:
            # Check if this is an IP or NS book
            is_ip = current.date_started is not None

            # Update estimated start date
            current.date_est_start = current_date

            # Calculate days estimate if not overridden
            if not current.days_estimate_override:
                current.days_estimate = self._calculate_days_estimate(current, reading_speeds)
            elif (is_ip and current.current_percent_manual_override and
                  current.current_percent is not None and
                  current.date_progress_set is not None and
                  current.book and current.book.word_count):
                # For IP books with a manual progress override, recalculate days_estimate
                # dynamically so that date_est_end stays current as time passes.
                # Without this, the end date gets stuck at the value computed when the
                # user last entered their progress and drifts into the past.
                current.days_estimate = self._calculate_ip_days_estimate_from_progress(
                    current, reading_speeds
                )

            # Update estimated end date
            if current.days_estimate:
                # For IP books, use ACTUAL start date (date_started)
                # For NS books, use estimated start date
                base_date = current.date_started if current.date_started else current_date

                # Round days_estimate to integer for date calculation
                # Subtract 1 because days are inclusive (3 days = start + 2)
                days_to_add = round(current.days_estimate) - 1
                current.date_est_end = base_date + timedelta(days=days_to_add)

                if is_ip:
                    # For IP books, next book starts the day after this one ends.
                    current_date = current.date_est_end + timedelta(days=1)
                else:
                    # NS book (#67 phase 3): the first not-started book of this format
                    # starts after the derived-PRIMARY in-progress book of this format
                    # finishes (wherever that book lives in the chains), or — if no book
                    # is primary in this format — on a rolling tomorrow. Later NS books in
                    # the chain follow sequentially.
                    if not seen_ns_book:
                        tomorrow = date.today() + timedelta(days=1)
                        anchor = primary_anchors.get(self._effective_format_lower(current))
                        current_date = max(anchor + timedelta(days=1), tomorrow) if anchor else tomorrow
                        current.date_est_start = current_date
                        current.date_est_end = current_date + timedelta(days=days_to_add)
                        seen_ns_book = True
                    # Next reading starts the day after this one ends
                    current_date = current.date_est_end + timedelta(days=1)
            else:
                current.date_est_end = None
                current_date += timedelta(days=1)  # Default to next day

            # Move to next reading in chain
            current = self._find_next_reading(current, chain_map)
    
    def _get_chain_start_date(self, head: Reading) -> date:
        """Get the start date for a chain."""
        today = date.today()

        # If the reading has an actual start date, use it
        if head.date_started:
            return head.date_started

        # For unstarted readings, always use today
        # This ensures that when we recalculate, unstarted books get updated to today
        return today

    def _started_est_end(self, reading: Reading, reading_speeds: Dict[str, int]) -> Optional[date]:
        """Estimated finish date for a STARTED book, priced at its primary format
        (#67). Mirrors the in-walk IP est_end computation (days_estimate selection +
        base = date_started), so anchors stay consistent with displayed dates."""
        if reading.date_started is None or not reading.book or not reading.book.word_count:
            return None
        if not reading.days_estimate_override:
            days = self._calculate_days_estimate(reading, reading_speeds)
        elif (reading.current_percent_manual_override and reading.current_percent is not None
              and reading.date_progress_set is not None):
            days = self._calculate_ip_days_estimate_from_progress(reading, reading_speeds)
        else:
            days = reading.days_estimate
        if not days:
            return None
        return reading.date_started + timedelta(days=round(days) - 1)

    def _apply_started_dates(self, reading: Reading, reading_speeds: Dict[str, int]):
        """Set est_start/est_end/days_estimate for an active in-progress book that lives
        on Home and is NOT part of a TBR chain (#65/#67). Start = its real start date;
        days_estimate priced at its primary format (same selection as the walk)."""
        if not reading.days_estimate_override:
            reading.days_estimate = self._calculate_days_estimate(reading, reading_speeds)
        elif (reading.current_percent_manual_override and reading.current_percent is not None
              and reading.date_progress_set is not None
              and reading.book and reading.book.word_count):
            reading.days_estimate = self._calculate_ip_days_estimate_from_progress(
                reading, reading_speeds)
        reading.date_est_start = reading.date_started
        if reading.days_estimate:
            reading.date_est_end = reading.date_started + timedelta(days=round(reading.days_estimate) - 1)
        else:
            reading.date_est_end = None

    def _compute_primary_anchors(
        self, readings: List[Reading], reading_speeds: Dict[str, int]
    ) -> Dict[str, date]:
        """For each format, the latest estimated-finish among STARTED books whose
        derived primary format is that format (#67 phase 3). Keyed by lowercased
        format; used to start each format's not-started chain."""
        anchors: Dict[str, date] = {}
        for r in readings:
            if r.date_started is None:
                continue
            est_end = self._started_est_end(r, reading_speeds)
            if est_end is None:
                continue
            fmt = self._effective_format_lower(r)  # derived primary for started books
            if not fmt:
                continue
            if fmt not in anchors or est_end > anchors[fmt]:
                anchors[fmt] = est_end
        return anchors

    def _effective_format_lower(self, reading: Reading) -> str:
        """Format to price an estimate at (#67). For a STARTED book, use its derived
        primary format (most cumulative word-equivalents) — a book declared as ebook
        but read mostly on audio should estimate at the audio WPD. For a not-started
        book there's no activity, so fall back to the declared media. Returns a
        lowercased key matching the reading_speeds dict."""
        if reading.date_started:
            primary = get_primary_format(self.db, reading.book_id, reading.media)
        else:
            primary = reading.media
        return (primary or reading.media or "").lower()

    def _calculate_ip_days_estimate_from_progress(
        self, reading: Reading, reading_speeds: Dict[str, int]
    ) -> Optional[int]:
        """Calculate days estimate for an IP book from its ACTUAL recorded progress.

        Re-evaluates how many days remain based on:
          - The real current percentage (tracked live from the reader, or set via the
            in-app progress control)
          - WPD for the reading's media type
        Recomputed whenever the chain is recalculated (i.e. whenever progress
        updates), so date_est_end and the rest of the format's chain stay accurate.

        NOTE: we deliberately do NOT project additional reading forward from the date
        the progress was recorded. Progress is directly tracked now, so the estimate
        is "elapsed so far + time to finish the remaining words at WPD" using the real
        position. (The old forward-projection — additional_words = days_since × wpd —
        is preserved here, commented out, for reference.)
        """
        today = date.today()
        days_elapsed = (today - reading.date_started).days + 1

        media_lower = self._effective_format_lower(reading)
        wpd = reading_speeds.get(media_lower, 12000)

        total_words = reading.book.word_count
        words_read = total_words * (reading.current_percent / 100.0)  # actual position

        # Legacy daily-goal projection (kept for reference, intentionally disabled):
        #   progress_date = reading.date_progress_set ...
        #   days_since_manual = (today - progress_date).days
        #   words_read = min(words_read + days_since_manual * wpd, total_words)

        words_remaining = max(0.0, total_words - words_read)
        if words_remaining <= 0:
            return days_elapsed

        days_remaining = math.ceil(words_remaining / wpd)
        return days_elapsed + days_remaining

    def _calculate_days_estimate(self, reading: Reading, reading_speeds: Dict[str, int]) -> Optional[int]:
        """Calculate estimated days to read based on book word count and media type."""
        if not reading.book or not reading.book.word_count or not reading.media:
            return None

        media_lower = self._effective_format_lower(reading)
        words_per_day = reading_speeds.get(media_lower, 12000)
        return math.ceil(reading.book.word_count / words_per_day)

    def _find_next_reading(self, current: Reading, chain_map: Dict[int, Reading]) -> Optional[Reading]:
        """Find the next reading in the chain."""
        for reading in chain_map.values():
            if reading.id_previous == current.id:
                return reading
        return None
    
    def finish_reading_and_start_next(self, reading_id: int):
        """Finish a reading and start the next one in the chain."""
        reading = self.db.query(Reading).filter(Reading.id == reading_id).first()
        if not reading:
            return
        
        # Find the next reading in the chain
        next_reading = self.db.query(Reading).filter(
            Reading.id_previous == reading_id
        ).first()
        
        if next_reading and not next_reading.date_started:
            # Start the next reading tomorrow
            tomorrow = date.today() + timedelta(days=1)
            next_reading.date_started = tomorrow
        
        # Recalculate all chains to update estimates
        self.recalculate_all_chains()
    
    def reorder_reading(self, reading_id: int, new_position: int):
        """Reorder a reading in the global TBR list."""
        reading = self.db.query(Reading).filter(Reading.id == reading_id).first()
        if not reading:
            return

        # Match the TBR-displayed set exactly (not-started + paused), since new_position
        # comes from that list. Active in-progress books now live on Home (#65) and must
        # NOT be in this index space, or the inserted book lands among them and the
        # media relink scrambles the order.
        all_readings = self.db.query(Reading).filter(
            Reading.date_finished_actual.is_(None),
            or_(
                Reading.date_started.is_(None),       # not started
                Reading.date_paused.isnot(None),      # or paused
            )
        ).all()

        # IP books should always come before NS books
        all_readings.sort(key=lambda r: (
            0 if r.date_started else 1,  # IP books (0) before NS books (1)
            r.date_started or r.date_est_start or date(2099, 1, 1),  # Then by date
            r.id  # Then by ID for stable sort
        ))

        # Remove the reading from its current position
        all_readings = [r for r in all_readings if r.id != reading_id]

        # Insert at new position
        if new_position >= len(all_readings):
            all_readings.append(reading)
        else:
            all_readings.insert(new_position, reading)

        # Now we need to rebuild the chain structure based on this new order
        # Strategy: Link each reading to the previous one of the same media type
        self._rebuild_chains_from_order(all_readings)

        # Recalculate dates
        self.recalculate_all_chains()
    
    def _get_chain_readings(self, reading: Reading) -> List[Reading]:
        """Get all readings in the same chain as the given reading."""
        # Find the head of the chain
        head = reading
        while head.id_previous:
            prev = self.db.query(Reading).filter(Reading.id == head.id_previous).first()
            if prev and prev.date_finished_actual is None:
                head = prev
            else:
                break
        
        # Collect all readings in the chain
        chain_readings = []
        current = head
        processed_ids = set()
        
        while current and current.id not in processed_ids:
            chain_readings.append(current)
            processed_ids.add(current.id)
            
            # Find next reading
            next_reading = self.db.query(Reading).filter(
                and_(
                    Reading.id_previous == current.id,
                    Reading.date_finished_actual.is_(None)
                )
            ).first()
            current = next_reading
        
        return chain_readings
    
    def _update_chain_links(self, chain_readings: List[Reading]):
        """Update the id_previous links for a chain of readings."""
        for i, reading in enumerate(chain_readings):
            if i == 0:
                # First reading - find its actual previous (might be a finished reading)
                # For now, we'll leave it as is to maintain connection to finished readings
                pass
            else:
                reading.id_previous = chain_readings[i - 1].id

    def _rebuild_chains_from_order(self, all_readings: List[Reading]):
        """Rebuild chain links based on a new global order.

        Each reading is linked to the previous reading of the same media type.
        This allows parallel reading of different media types.
        """
        # Group by media type and track the last reading of each type
        last_by_media = {}

        for reading in all_readings:
            media = reading.media or 'Unknown'

            if media in last_by_media:
                # Link to the previous reading of the same media type
                reading.id_previous = last_by_media[media].id
            else:
                # First reading of this media type - check if there's a finished reading to link to
                # For now, we'll set to None (could be enhanced to find last finished of same media)
                reading.id_previous = None

            # Update the last reading of this media type
            last_by_media[media] = reading

    def handle_format_change(self, reading_id: int, old_media: str, new_media: str):
        """Handle a reading's format/media change by moving it to the appropriate chain.

        This method handles two scenarios:
        1. IP (in-progress) books: Always become the head of the new chain (IP > NS priority)
        2. NS (not-started) books: Preserve position based on estimated start date

        Strategy:
        1. Remove the reading from its old chain
        2. If IP: Insert as head of new chain (or after existing IP books)
           If NS: Find position based on date_est_start
        3. Recalculate all chains to update dates

        Args:
            reading_id: The ID of the reading being changed
            old_media: The previous media type
            new_media: The new media type
        """
        reading = self.db.query(Reading).filter(Reading.id == reading_id).first()
        if not reading:
            return

        # Check if this reading is in progress
        is_in_progress = reading.date_started is not None

        # Store the current estimated start date to preserve position (for NS books)
        target_start_date = reading.date_est_start or reading.date_started

        # Get all unfinished readings
        all_unfinished = self.db.query(Reading).filter(
            Reading.date_finished_actual.is_(None)
        ).all()

        # Step 1: Remove from old chain
        # Find what was pointing to this reading in the old chain
        for r in all_unfinished:
            if r.id_previous == reading_id:
                # Point it to what we were pointing to instead (bypass us)
                r.id_previous = reading.id_previous
                break

        # Step 2: Find the appropriate position in the new chain
        # Get all readings in the new chain (excluding the one being moved)
        new_chain_readings = [r for r in all_unfinished if r.media == new_media and r.id != reading_id]

        if not new_chain_readings:
            # New chain is empty, this reading becomes the head
            reading.id_previous = None
        elif is_in_progress:
            # This is an IP book - it should be inserted among other IP books
            # IP books are ordered by start date (earliest first)
            # All NS books come after all IP books

            # Find any existing IP books in the new chain
            existing_ip_books = [r for r in new_chain_readings if r.date_started is not None]

            if not existing_ip_books:
                # No IP books in new chain, this becomes the head
                # Find the current head (which must be NS) and point it to this reading
                new_chain_head = next((r for r in new_chain_readings
                                      if not r.id_previous or r.id_previous not in [ur.id for ur in new_chain_readings]), None)
                if new_chain_head:
                    new_chain_head.id_previous = reading.id
                reading.id_previous = None
            else:
                # There are existing IP books - insert in order by start date
                # Sort IP books by start date (earliest first)
                existing_ip_books.sort(key=lambda r: (
                    r.date_started or date(2099, 1, 1),
                    r.id
                ))

                # Find where this reading should be inserted based on its start date
                insert_after = None
                for ip_book in existing_ip_books:
                    if reading.date_started and ip_book.date_started and reading.date_started > ip_book.date_started:
                        insert_after = ip_book
                    else:
                        break

                if insert_after is None:
                    # This reading started earliest, so it becomes the new head
                    # The current first IP book should point to this reading
                    first_ip_book = existing_ip_books[0]
                    first_ip_book.id_previous = reading.id
                    reading.id_previous = None
                else:
                    # Insert after the identified IP book
                    # Find what was pointing to insert_after
                    next_reading = next((r for r in new_chain_readings if r.id_previous == insert_after.id), None)
                    if next_reading:
                        # Insert between insert_after and next_reading
                        next_reading.id_previous = reading.id
                    reading.id_previous = insert_after.id
        else:
            # This is an NS book - preserve its position based on estimated start date
            # Sort new chain readings by estimated start date
            new_chain_readings.sort(key=lambda r: (
                r.date_est_start or r.date_started or date(2099, 1, 1),
                r.id
            ))

            # Find the position where this reading should be inserted
            # based on its target start date
            insert_after = None

            if target_start_date:
                # Find the last reading that should come before this one
                for r in new_chain_readings:
                    r_start = r.date_est_start or r.date_started
                    if r_start and r_start < target_start_date:
                        insert_after = r
                    else:
                        break

            if insert_after is None:
                # Insert at the beginning of the chain
                # Find the current head
                new_chain_head = next((r for r in new_chain_readings
                                      if not r.id_previous or r.id_previous not in [ur.id for ur in new_chain_readings]), None)
                if new_chain_head:
                    new_chain_head.id_previous = reading.id
                reading.id_previous = None
            else:
                # Insert after the identified reading
                # Find what was pointing to insert_after
                next_reading = next((r for r in new_chain_readings if r.id_previous == insert_after.id), None)
                if next_reading:
                    # Insert between insert_after and next_reading
                    next_reading.id_previous = reading.id
                reading.id_previous = insert_after.id

        # Step 2.5: If this is an IP book with manual progress, recalculate days_estimate based on new WPD
        if is_in_progress and reading.current_percent is not None and reading.book:
            import math
            from datetime import date as dt
            from ..services.settings_service import get_wpd_for_media

            today = dt.today()
            days_elapsed = (today - reading.date_started).days + 1

            # Get WPD for the NEW media type
            wpd = get_wpd_for_media(self.db, new_media)

            # Calculate remaining words and days
            total_words = reading.book.word_count
            words_read = total_words * (reading.current_percent / 100.0)
            words_remaining = total_words - words_read

            days_remaining = math.ceil(words_remaining / wpd)
            new_days_estimate = days_elapsed + days_remaining

            # Update days_estimate
            reading.days_estimate = new_days_estimate
            reading.days_estimate_override = True

        # Commit the chain link changes
        self.db.commit()

        # Step 3: Recalculate all chains to update dates
        self.recalculate_all_chains()
