# GreatReads Integration Reference

> **STATUS (Story 2 — DONE, 2026-06-15):** The integration now runs entirely against the
> **local vendored GreatReads on `:8092`** (`greatreads_ereader` container, repo-local DB at
> `greatreads/data/greatreads.db`). The old remote prod **`:8007` has been retired** —
> `greatreads_app` is stopped, its data dir kept as a cold backup. Reading progress is written
> straight into that DB (see [`backend/server.py`](backend/server.py) `_gr_set_current_percent`
> and the `ereader_progress` table), so the old title-matching "sync" job is gone. The base URL
> below and any remaining `:8007` references are historical.

**GreatReads Source Code**: vendored in-repo at `greatreads/` (was `../GreatReads/`, source commit `dbafbc1 v2.1.7`)

## CRITICAL RULES FOR AGENTS

### 1. NEVER GUESS — ALWAYS RESEARCH

When working with GreatReads integration:
- **DO NOT** assume field names, data structures, or API behavior
- **DO NOT** guess about how the TBR queue works
- **DO NOT** make up query parameters or endpoint behavior

**ALWAYS**:
1. Check `../GreatReads/` source code first
2. Look at the database schema: `../GreatReads/src/greatreads.db`
3. Read the chain system docs: `../GreatReads/CHAIN_SYSTEM_ANALYSIS.md` (symlinked here as `GREATREADS_CHAIN_REFERENCE.md`)
4. Verify actual API endpoints and response shapes

### 2. Understanding the Chain Structure

GreatReads uses **linked-list chains** for reading order, one per media format (Physical/Ebook/Audio).

**Key Concept**: Each reading has an `id_previous` field that points to the PREVIOUS reading in the chain.

```
Reading A (id=100, id_previous=NULL)  ← Chain head
    ↓ (Reading B has id_previous=100)
Reading B (id=200, id_previous=100)
    ↓ (Reading C has id_previous=200)
Reading C (id=300, id_previous=200)
```

**To find the NEXT book after Reading B**:
- Query all readings
- Find the one where `id_previous == 200` (that's Reading C)

**NOT**: Query for `status=in_progress` or `rank` or any other field. The chain is defined ONLY by `id_previous` links.

## Database Schema Reference

**Table**: `read` (in `../GreatReads/src/greatreads.db`)

**Chain-critical fields**:
- `id` — Primary key
- `id_previous` — Foreign key to `read.id` (defines the chain)
- `media` — "Physical", "Ebook", or "Audio"

**Date fields**:
- `date_started` — User-set actual start date
- `date_finished_actual` — User-set actual finish date
- `date_est_start` — Calculated by chain calculator
- `date_est_end` — Calculated by chain calculator

**Rating fields** (all floats, 0-10 scale):
- `rating_overall`
- `rating_horror`
- `rating_spice`
- `rating_world_building`
- `rating_writing`
- `rating_characters`
- `rating_readability`
- `rating_enjoyment`

**Other fields**:
- `book_id` — FK to books table
- `rank` — Separate ordering (NOT used for chains)
- `status` — Computed status (not a real DB field in some GreatReads versions, inferred from dates)
- `days_estimate` — Calculated reading time estimate
- `reread` — Boolean flag

## GreatReads API Endpoints (Used by Ereader)

**Base URL**: `http://127.0.0.1:8092` (local vendored instance; was `http://100.69.184.113:8007` before Story 2)

### Reading CRUD
- `GET  /api/readings/` — List all readings. Query params: `media`, `status` (computed), `book_title`
- `GET  /api/readings/{id}/` — Get single reading
- `POST /api/readings/` — Create reading
- `PUT  /api/readings/{id}/` — Full update
- `PATCH /api/readings/{id}/` — Partial update (use this to start a reading: `{date_started, status}`)

### Books
- `GET /api/books/` — List books. Query params: `title`
- `GET /api/books/{id}/` — Get single book

## Ereader → GreatReads Integration Flow

### "Mark as Finished" Feature (`POST /api/greatreads/finish`)

1. **Frontend opens dialog**: Calls `GET /api/greatreads/format/<bookId>` to get current format and readingId
2. **User fills ratings**: 7 ratings (0-10 integers) + finish date
3. **Frontend submits**: `POST /api/greatreads/finish` with `{bookId, title, author, media, finishDate, rating, ratings, readingId}`

**Backend flow**:
1. Fetch the current reading (by `readingId` if provided, else search by title+media)
2. **BEFORE marking as finished**: Find next book in chain
   - Query `GET /api/readings/?media={media}` (all readings, not just in-progress)
   - Find the reading where `id_previous == current_reading.id`
   - Store the next reading's ID and book info
3. Mark current reading as finished: `PUT /api/readings/{id}/` with `{status: "finished", date_finished_actual, rating_overall, rating_*}`
4. **Start the next reading**: `PATCH /api/readings/{next_id}/` with `{date_started: finish_date, status: "in_progress"}`
5. Clear local progress for the finished book
6. Return `{success, readingId, message, nextBook}` to frontend

### Why This Order Matters

- The next book in the chain is **not started yet** when you finish the current book
- You can't query for `status=in_progress` to find it — it won't be there
- You MUST query the chain structure (`id_previous` links) to find it
- Then START it as part of the finish flow

## Common Mistakes to Avoid

❌ **Querying for next in-progress book**: `GET /api/readings/?status=in_progress&media=Audio`
   - The next book isn't in-progress yet — you haven't started it!

❌ **Assuming `rank` defines reading order**
   - `rank` is a separate field, not used for chains

❌ **Guessing at field names**
   - Always check the actual schema

✅ **Query all readings and find the chain link**: `GET /api/readings/?media=Audio`, then find where `id_previous == current_id`

✅ **Start the next reading explicitly**: `PATCH /api/readings/{next_id}/` with start date and status

## Reference Files

- **Chain system deep-dive**: `GREATREADS_CHAIN_REFERENCE.md` (symlink to `../GreatReads/CHAIN_SYSTEM_ANALYSIS.md`)
- **GreatReads source**: `GREATREADS_SOURCE/` (symlink to `../GreatReads/`)
- **This project's integration code**: `backend/server.py` (search for "GreatReads")
