# Offline Reading Support - Implementation Plan

## Current State

### What Already Works
- **IndexedDB caching**: Books (EPUB blobs) are cached automatically when opened via `reader.html` → `putCachedBookBlob`
- **Cover caching**: Covers are cached in a separate object store, lazy-loaded via IntersectionObserver
- **localStorage position**: Reading position is saved locally first, then synced to backend
- **Progress sync graceful degradation**: Backend sync failures are caught silently, reading position never lost

### What Doesn't Work Offline
- **Library page**: `index.html` → `loadBooks()` shows error when `/api/library` fails
- **No metadata in cache**: IndexedDB stores `{id, format, blob, cached}` but NOT title/author
- **No offline book list**: Can't display cached books when backend is down
- **Search**: Completely backend-dependent
- **Audiobooks**: Require live ABS session (HLS stream or direct play) - can't work offline

## Problems with Previous Attempt

1. **`AbortSignal.timeout()` not supported** - Broke in Android WebView (added in Chrome 103, WebView may be older)
2. **Too many changes at once** - Hard to debug when everything breaks
3. **Function ordering** - Called `fetchWithTimeout` before defining it
4. **No incremental testing** - Didn't verify each change worked before moving to next

## Better Implementation Plan

### Phase 1: Metadata Storage (Backend + Reader)
**Goal**: Store title/author when caching books so offline display is possible.

1. **Update `reader.html` → `putCachedBookBlob`**:
   - Add `title` and `author` to the IndexedDB record
   - These are already available as `bookTitle` and `bookAuthor` params

2. **Test**: Open a book, verify IndexedDB record includes metadata

### Phase 2: Offline Book List (Frontend Only)
**Goal**: Show cached books when backend is unreachable.

1. **Add `loadOfflineBooks()` function**:
   - Reads IndexedDB `books` store
   - Returns array of minimal book objects with title/author from cache
   - Returns empty array if DB not ready

2. **Update `loadBooks()` catch block**:
   - Try `loadOfflineBooks()` on network failure
   - Display with a subtle offline indicator
   - Use **manual AbortController** not `AbortSignal.timeout()`:
     ```js
     const controller = new AbortController();
     const timeout = setTimeout(() => controller.abort(), 5000);
     fetch(url, { signal: controller.signal })
       .finally(() => clearTimeout(timeout));
     ```

3. **Add offline indicator**:
   - Change status-bar clock color to red when offline
   - Tooltip: "Offline - showing cached books only"

4. **Test each change**:
   - Define functions, verify no syntax errors
   - Test with backend running (should work normally)
   - Test with backend stopped (should show cached books)
   - Force-close app between tests

### Phase 3: Offline Search (Frontend Only)
**Goal**: Local filter over cached books when search fails.

1. **Update `runServerSearch()` catch block**:
   - On failure, call `loadOfflineBooks()`
   - Filter by title/author substring match (case-insensitive)
   - Display results with offline indicator

2. **Test**: Stop backend, search for a cached book

### Phase 4: Polish
- Better error messages ("Offline - showing X cached books")
- Clear distinction between "offline" and "no books cached"
- Offline state persists across app restarts until backend comes back

## Implementation Rules

### DO
✓ Make ONE change at a time
✓ Test after EACH change (force-close app!)
✓ Use manual AbortController + setTimeout for timeouts
✓ Define helper functions at the TOP of the script block
✓ Add `.catch(() => {})` to all network calls
✓ Commit working state before next change
✓ Keep backend and frontend changes separate

### DON'T
✗ Use `AbortSignal.timeout()` - not supported everywhere
✗ Make 10 changes and test once
✗ Assume functions are hoisted - they're not if defined as `const fn = ...`
✗ Edit both backend and frontend in same session
✗ Forget to force-close app when testing frontend changes
✗ Skip the curl tests before touching code

## Testing Checklist

Before starting ANY code change:

```bash
# 1. Verify clean baseline
curl -s http://localhost:8091/api/health
curl -s http://100.69.184.113:8090/ | head -10
# Open app, verify library loads

# 2. Make ONE change to ONE file

# 3. Test syntax
node -e "require('fs').readFileSync('web/index.html', 'utf8')"

# 4. Restart services if needed
# Backend: kill + ./run.sh
# Static: kill + serve.py

# 5. Force-close app, reopen, verify still works

# 6. Test offline scenario
# Stop backend, reload app, verify graceful degradation

# 7. Commit if working
git add <file>
git commit -m "Add <feature> - tested"
```

## Fallback Strategy

If at ANY point the app breaks and you can't fix it in 5 minutes:

```bash
git checkout .
# Follow RECOVERY.md
# Start over with smaller change
```

## Future Enhancements (Not Now)

- Service Worker for true offline-first architecture
- Background sync to upload progress when back online
- Download books in bulk for offline trips
- Offline Series/Saga views (cache `/api/series` response)
- Download queue UI

## Key Insight from Failure

**The WebView is unforgiving.** A single JavaScript error makes the entire page black with no visible error to the user. The only way to debug is:

1. Small, incremental changes
2. Test immediately after each change
3. Force-close app to clear cache
4. Keep `git checkout .` ready to escape

