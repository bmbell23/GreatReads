#!/usr/bin/env python3
"""#274 — recover ebook days that were dropped because the reading's date_started
was stamped in the future.

A client that built "today" from `toISOString()` (UTC) stamped `date_started` a day
ahead all evening. `_book_in_progress_on` then failed for that day's sessions, so
`_rederive_ebook_activity_row` DELETED the daily rollup row instead of writing it —
the reading time is still in `reading_sessions`, it just never reached
`reading_activity`, so Home's daily goals and Stats show nothing for the day.

This finds every such day, pulls `date_started` back to the earliest day that has a
qualifying session, and re-derives the rollup rows from the sessions themselves.

Dry-run by default; `--apply` backs the DB up first, then writes.
"""
import argparse, os, shutil, sqlite3, sys, time

DB = os.environ.get('GR_DB', '/home/brandon/projects/GreatReads/greatreads/data/greatreads.db')
SESSION_MIN_MS = 60_000     # mirrors ereader_api._SESSION_MIN_MS
EBOOK_MAX_WPM = 2000.0      # mirrors ereader_api._EBOOK_MAX_WPM
QUALIFIES = ("(COALESCE(ended_at,0) - COALESCE(started_at,0)) >= ? "
             "AND minutes > 0 AND (words = 0 OR (words * 1.0 / minutes) <= ?)")


def find_lost(conn):
    """Days with qualifying ebook sessions but no reading_activity row, where the
    reading's date_started is later than the day (the #274 signature)."""
    rows = conn.execute(
        "SELECT s.activity_date AS d, s.book_key AS bk, "
        "       SUM(s.minutes) AS m, SUM(s.words) AS w, "
        "       SUM(s.wpm_mpw_sum) AS ws, SUM(s.wpm_n) AS wn "
        "FROM reading_sessions s "
        "WHERE s.format='Ebook' AND s.activity_date IS NOT NULL AND " + QUALIFIES + " "
        "AND NOT EXISTS (SELECT 1 FROM reading_activity a "
        "                WHERE a.activity_date=s.activity_date AND a.book_key=s.book_key "
        "                  AND a.format='Ebook') "
        "GROUP BY s.activity_date, s.book_key ORDER BY s.activity_date",
        (SESSION_MIN_MS, EBOOK_MAX_WPM)).fetchall()

    lost = []
    for r in rows:
        bk = r['bk']
        source, ext_id = ('audiobookshelf', bk[4:]) if bk.startswith('abs:') else ('calibre', bk)
        reading = conn.execute(
            "SELECT r.id, r.date_started, b.title FROM read r "
            "JOIN external_imports ei ON ei.book_id = r.book_id "
            "JOIN books b ON b.id = r.book_id "
            "WHERE ei.source=? AND ei.external_id=? AND r.date_started IS NOT NULL "
            "AND date(r.date_started) > date(?) "
            "AND (r.date_finished_actual IS NULL OR date(r.date_finished_actual) >= date(?)) "
            "LIMIT 1", (source, ext_id, r['d'], r['d'])).fetchone()
        if reading:
            lost.append((r, reading))
    return lost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write the repair (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    lost = find_lost(conn)
    if not lost:
        print('Nothing to repair — no ebook days lost to a future date_started.')
        return 0

    print(f'{len(lost)} lost ebook day(s):\n')
    for r, reading in lost:
        print(f"  {r['d']}  {reading['title'][:44]:<44} "
              f"{r['m']:6.1f} min  {int(r['w']):>6} words   "
              f"(reading {reading['id']}: date_started {reading['date_started']} -> {r['d']})")

    if not args.apply:
        print('\nDry run — re-run with --apply to write.')
        return 0

    backup = f"{DB}.bak-274-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(DB, backup)
    print(f'\nBacked up {DB} -> {backup}')

    with conn:
        for r, reading in lost:
            # Earliest qualifying session day for this book — pull the start date back
            # to that, not just to the day being repaired.
            first = conn.execute(
                "SELECT MIN(activity_date) d FROM reading_sessions "
                "WHERE book_key=? AND format='Ebook' AND activity_date IS NOT NULL AND " + QUALIFIES,
                (r['bk'], SESSION_MIN_MS, EBOOK_MAX_WPM)).fetchone()['d']
            start = min(first or r['d'], r['d'])
            if start < reading['date_started']:
                conn.execute("UPDATE read SET date_started=? WHERE id=?", (start, reading['id']))
                print(f"  reading {reading['id']}: date_started {reading['date_started']} -> {start}")
            conn.execute(
                "INSERT INTO reading_activity(activity_date,book_key,format,minutes,words,wpm_mpw_sum,wpm_n) "
                "VALUES(?,?,'Ebook',?,?,?,?) ON CONFLICT(activity_date,book_key,format) DO UPDATE SET "
                " minutes=excluded.minutes, words=excluded.words, "
                " wpm_mpw_sum=excluded.wpm_mpw_sum, wpm_n=excluded.wpm_n",
                (r['d'], r['bk'], float(r['m'] or 0), int(r['w'] or 0),
                 float(r['ws'] or 0), int(r['wn'] or 0)))
            print(f"  credited {r['d']} {r['bk']}: {r['m']:.1f} min / {int(r['w'])} words")

    print('\nDone. Verify with: GET /api/stats/activity')
    return 0


if __name__ == '__main__':
    sys.exit(main())
