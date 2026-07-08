// GreatReads audiobook player. Talks to the Flask backend's ABS session proxy:
//   POST /api/audiobooks/<absId>/play        -> start session, get track URLs
//   POST /api/audiobooks/sessions/<sid>/sync  -> report progress (delta seconds)
//   POST /api/audiobooks/sessions/<sid>/close -> end session
// The proxy hands back absolute, token-bearing media URLs; we never see the
// ABS token. HLS (transcode) is played via hls.js; single-file direct play
// feeds the <audio> element straight.
const API_URL = 'http://100.69.184.113:8092/api';
const params = new URLSearchParams(location.search);
const ABS_ID = params.get('absId') || '';
const TITLE = params.get('title') || '';
const AUTHOR = params.get('author') || '';
// Optional ebook link (dual-format works). When present we can open the EPUB
// reader / in-book search in an overlay without leaving the player — the
// <audio> in THIS document keeps playing.
const EBOOK_ID = params.get('bookId') || '';
const EBOOK_FORMAT = params.get('format') || 'epub';
const HAS_EBOOK = params.get('hasEbook') === '1' && !!EBOOK_ID;

// We persist resume position to our own backend. For dual-format books
// (has both ebook and audiobook), we key progress by the CALIBRE book ID
// so ebook and audiobook share one unified progress record. For audio-only
// books, we use "abs:<absId>". This ensures one progress record per book
// regardless of which format you're using. Speed is a global user preference.
const PROGRESS_KEY = HAS_EBOOK && EBOOK_ID ? EBOOK_ID : (ABS_ID ? ('abs:' + ABS_ID) : '');
const SPEED_KEY = 'ereader.audio.speed';

// #248: audio credit high-water-mark (seconds). When the live position is below it we
// aren't earning credit → the sync button goes yellow-orange. Loaded best-effort, kept
// monotonic in updateUI, reset to the current spot when the user taps sync.
let _maxPos = 0;
if (PROGRESS_KEY) fetch(`${API_URL}/progress/${encodeURIComponent(PROGRESS_KEY)}`)
    .then(r => r.ok ? r.json() : null)
    .then(d => { if (d && typeof d.maxPosition === 'number') _maxPos = d.maxPosition; })
    .catch(() => {});

// Cross-instance heartbeat (#207). A fold-close can spawn a SECOND WebView whose
// rehydrated player boots paused at the last periodic save while the old
// instance's audio keeps playing (localStorage is shared across instances, so
// they can see each other). A PLAYING player stamps its live position every few
// seconds; a BOOTING player that finds a fresh playing heartbeat for the same
// book adopts the live position instead of the stale save. The real fix (one
// activity instance, APK singleTask) prevents the ghost; this makes any residual
// ghost harmless.
const HB_KEY = 'gr.playerHeartbeat';
let hbTimer = null;
function stampHeartbeat(playing) {
    if (!PROGRESS_KEY) return;
    try {
        localStorage.setItem(HB_KEY, JSON.stringify({
            key: PROGRESS_KEY, pos: globalTime(), playing: !!playing, at: Date.now(),
        }));
    } catch (_) {}
}
function readLiveHeartbeat() {
    try {
        const h = JSON.parse(localStorage.getItem(HB_KEY) || 'null');
        if (h && h.key === PROGRESS_KEY && h.playing && h.pos > 0
                && (Date.now() - h.at) < 20000) return h;
    } catch (_) {}
    return null;
}
function startHb() { stampHeartbeat(true); if (!hbTimer) hbTimer = setInterval(() => stampHeartbeat(!audio.paused && !audio.ended), 5000); }
function stopHb() { if (hbTimer) { clearInterval(hbTimer); hbTimer = null; } stampHeartbeat(false); }

// Multi-part edition (e.g. a dramatized adaptation split across 2 ABS items, or
// a long unabridged audiobook split into 5). The library stashes the chosen
// edition in localStorage before navigating here; we then play its parts
// back-to-back as one continuous book. Absent -> single-part playback of ABS_ID.
const EDITION_ID = params.get('edition') || '';
function loadEdition() {
    if (!EDITION_ID) return null;
    try {
        const raw = localStorage.getItem('ereader.player.ed:' + EDITION_ID);
        const ed = raw ? JSON.parse(raw) : null;
        if (ed && (ed.parts || []).length) return ed;
    } catch (_) {}
    return null;
}

const $ = (id) => document.getElementById(id);
const audio = $('audio');

// Mark this audiobook as open so a cold WebView relaunch lands back in the
// player instead of Home (#198). Cleared on real navigation away, refreshed on
// backgrounding + periodic saves — see active-book.js.
if (window.ActiveBook) ActiveBook.trackPage();

// Session state (always reflects the CURRENTLY-loaded part).
let session = null, sid = null, chapters = [], duration = 0;
let hls = null, lastSyncAt = 0, syncTimer = null, pendingSeek = 0;
// True once the media has loaded and any resume seek has been applied. Until
// then we must NOT persist progress — currentTime sits at 0 during the load
// window, and saving it would clobber the real saved position (a big cause of
// "lost my place"), especially if loading is slow or the user leaves early.
let resumeApplied = false;
let scrubbing = false, chScrubbing = false, closed = false;
let chosenRate = 1;  // user-selected playback speed, re-applied on (re)load

// Edition / parts model. Single-part playback is just PARTS=[{absId:ABS_ID}].
// `duration` above is the current part; the BOOK total is editionDuration.
let EDITION = null;
let PARTS = [{ absId: ABS_ID, duration: 0 }];
let curPart = 0;          // index of the loaded part within PARTS
let partStarts = [0];     // cumulative global start time (sec) of each part
let editionDuration = 0;  // sum of all part durations (0 until known)

function fmt(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const mm = (h && m < 10) ? '0' + m : String(m);
    return (h ? h + ':' : '') + mm + ':' + String(sec).padStart(2, '0');
}

function setMsg(text) {
    const el = $('overlay-msg');
    // Any real message (errors) or a dismiss stops the loading-quote rotation
    // (#55) so it can't paint over the message / a started player.
    if (window.GreatReadsQuotes) GreatReadsQuotes.stop();
    if (!text) { el.classList.add('hidden'); return; }
    el.textContent = text;
    el.classList.remove('hidden');
}

// Loading state: show saved-highlight quotes instead of a bare "Loading…"
// while the ABS session spins up (#55). Idempotent via GreatReadsQuotes.start.
function setLoading() {
    const el = $('overlay-msg');
    el.classList.remove('hidden');
    if (window.GreatReadsQuotes) GreatReadsQuotes.start(el, API_URL);
    else el.textContent = 'Loading…';
}

// Lightweight auto-dismissing toast (bottom-centre). Created lazily so we don't
// need a dedicated element in the markup.
let _toastTimer = null;
function toast(msg) {
    let el = $('player-toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'player-toast';
        el.style.cssText = 'position:fixed;left:50%;bottom:96px;transform:translateX(-50%);'
            + 'background:#212529;color:#fff;padding:10px 16px;border-radius:20px;font-size:14px;'
            + 'box-shadow:0 4px 16px rgba(0,0,0,0.25);z-index:200;opacity:0;transition:opacity .2s;'
            + 'max-width:80vw;text-align:center;pointer-events:none;';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    requestAnimationFrame(() => { el.style.opacity = '1'; });
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 1800);
}

// Paint the filled portion of a range input with the brand gradient up to the
// thumb, leaving the rest as the dark track. Called whenever the value changes.
function fillRange(el) {
    if (!el) return;
    const min = parseFloat(el.min) || 0, max = parseFloat(el.max) || 100;
    const pct = max > min ? ((parseFloat(el.value) - min) / (max - min)) * 100 : 0;
    el.style.background =
        `linear-gradient(90deg, var(--brand-blue) 0%, var(--brand-pink) ${pct}%, var(--track) ${pct}%, var(--track) 100%)`;
}

function currentRate() { return audio.playbackRate || 1; }

function loadSavedSpeed() {
    let r = parseFloat(localStorage.getItem(SPEED_KEY));
    if (!isFinite(r)) r = 1;
    return Math.min(3, Math.max(1, Math.round(r * 10) / 10));
}
// Apply + persist a playback speed and reflect it in the slider UI.
function applySpeed(r) {
    r = Math.min(3, Math.max(1, Math.round((parseFloat(r) || 1) * 10) / 10));
    chosenRate = r;
    audio.playbackRate = r;
    const lbl = r.toFixed(1) + '×';
    const sp = $('speed'); if (sp) sp.value = r;
    const btn = $('speed-btn'); if (btn) btn.textContent = lbl;       // #265: button shows current speed
    const pv = $('speed-popup-val'); if (pv) pv.textContent = lbl;
    try { localStorage.setItem(SPEED_KEY, String(r)); } catch (_) {}
    updateUI();
}

// ---------- Local audio cache (read side, #54) ----------
// Plays an in-progress audiobook from the file the library pre-downloaded into
// IndexedDB (EreaderDB v3) — no ABS /play session, no streaming — so it's
// instant and works in a dead zone. Single-file books only for now; multi-file
// cleanly falls through to streaming. Best-effort: any miss returns null.
let _audioDB = null, _audioDBPromise = null;
function openAudioDB() {
    if (_audioDB) return Promise.resolve(_audioDB);
    if (_audioDBPromise) return _audioDBPromise;
    // Open WITHOUT a version so we never trigger an upgrade or create an empty
    // store-less DB that would break the library's schema (the library/reader
    // own EreaderDB's v3 schema; we're a read-only guest here).
    _audioDBPromise = new Promise((resolve) => {
        let req;
        try { req = indexedDB.open('EreaderDB'); } catch (_) { return resolve(null); }
        req.onsuccess = (e) => { _audioDB = e.target.result; resolve(_audioDB); };
        req.onerror = () => resolve(null);
    });
    return _audioDBPromise;
}
function _idbGet(store, key) {
    return openAudioDB().then((dbh) => new Promise((resolve) => {
        if (!dbh || !dbh.objectStoreNames.contains(store)) return resolve(null);
        try {
            const r = dbh.transaction([store], 'readonly').objectStore(store).get(key);
            r.onsuccess = () => resolve(r.result || null);
            r.onerror = () => resolve(null);
        } catch (_) { resolve(null); }
    }));
}
async function getCachedManifest(absId) {
    const rec = await _idbGet('cacheMeta', 'manifest:' + absId);
    return rec && rec.manifest ? rec.manifest : null;
}
async function getCachedTrackBlob(absId, ino) {
    const rec = await _idbGet('audio', absId + ':' + ino);
    return rec && rec.blob ? rec.blob : null;
}
// Build a /play-shaped session from the local cache for a SINGLE-FILE audiobook.
// Returns null if not fully cached or multi-file (caller then streams).
async function loadLocalSession(absId) {
    try {
        const manifest = await getCachedManifest(absId);
        if (!manifest) return null;
        const tracks = manifest.tracks || [];
        if (tracks.length !== 1) return null;          // single-file only (for now)
        const t = tracks[0];
        const blob = await getCachedTrackBlob(absId, t.ino);
        if (!blob) return null;
        return {
            id: null,                                   // no ABS session → sync/close no-op
            audioTracks: [{ contentUrl: URL.createObjectURL(blob) }],
            chapters: manifest.chapters || [],
            duration: t.duration || manifest.totalDuration || 0,
            currentTime: 0, startTime: 0, _local: true,
        };
    } catch (_) { return null; }
}

// ---------- Local audio cache (WRITE side, #261) ----------
// The player used to only READ the cache; a book that wasn't pre-downloaded on
// the Home screen (e.g. you resumed straight into it, #198) then streamed
// forever. Now the player caches this book on open too — every part's tracks +
// manifest, plus the linked ebook — so the next open is fully local. All
// fire-and-forget + delayed so it never competes with playback start,
// skip-if-cached, and storage-guarded.
function _idbPut(store, value) {
    return openAudioDB().then((dbh) => new Promise((resolve) => {
        if (!dbh || !dbh.objectStoreNames.contains(store)) return resolve(false);
        try {
            const tx = dbh.transaction([store], 'readwrite');
            tx.objectStore(store).put(value);
            tx.oncomplete = () => resolve(true);
            tx.onerror = () => resolve(false);
            tx.onabort = () => resolve(false);
        } catch (_) { resolve(false); }
    }));
}
async function _cacheAudiobookParts() {
    for (const p of (PARTS || [])) {
        const absId = p && p.absId;
        if (!absId) continue;
        try {
            const mr = await fetch(`${API_URL}/audiobooks/${encodeURIComponent(absId)}/tracks`);
            if (!mr.ok) continue;
            const manifest = await mr.json();
            await _idbPut('cacheMeta', { id: 'manifest:' + absId, manifest, cachedAt: Date.now() });
            const tracks = manifest.tracks || [];
            // Storage guard — don't start a big download unless it'll actually fit.
            const need = tracks.reduce((s, t) => s + (t.size || 0), 0);
            if (navigator.storage && navigator.storage.estimate) {
                try {
                    const est = await navigator.storage.estimate();
                    const free = (est.quota || 0) - (est.usage || 0);
                    if (need > 0 && free > 0 && need > free * 0.9) continue;
                } catch (_) {}
            }
            for (const t of tracks) {
                if (await getCachedTrackBlob(absId, t.ino)) continue;   // skip cached
                const r = await fetch(t.url);
                if (!r.ok) continue;
                const blob = await r.blob();
                await _idbPut('audio', {
                    id: absId + ':' + t.ino, absId, ino: t.ino, blob,
                    size: blob.size, mime: blob.type || t.mime || 'audio/mpeg',
                    cachedAt: Date.now(),
                });
            }
        } catch (_) { /* try again on a later open */ }
    }
}
async function _cacheLinkedEbook() {
    if (!linkedEbookId) return;
    try {
        const existing = await _idbGet('books', linkedEbookId);
        if (existing && existing.blob) return;                  // already cached
        const r = await fetch(`${API_URL}/ebooks/${encodeURIComponent(linkedEbookId)}/download?format=epub`);
        if (!r.ok) return;
        const blob = await r.blob();
        await _idbPut('books', { id: linkedEbookId, format: 'epub', blob, cached: new Date() });
    } catch (_) {}
}
async function precacheThisBook() {
    await _cacheAudiobookParts();
    await _cacheLinkedEbook();
}

async function init() {
    $('title').textContent = TITLE;
    $('author').textContent = AUTHOR;
    // Start (or coalesce) the listening session for this sitting (#57).
    if (PROGRESS_KEY) initSession();
    // Saved-highlight quotes while we resolve resume + spin up the ABS session.
    setLoading();
    // #223: ALWAYS prefer the GreatReads cover (matched by title+author) — ABS
    // artwork is only the fallback when we have no stored cover for this book.
    {
        const cov = $('cover');
        const absCover = ABS_ID ? `${API_URL}/audiobooks/${encodeURIComponent(ABS_ID)}/cover` : '';
        if (TITLE) {
            cov.onerror = () => { cov.onerror = null; if (absCover) cov.src = absCover; };
            cov.src = `${API_URL}/books/cover-by-title?title=${encodeURIComponent(TITLE)}&author=${encodeURIComponent(AUTHOR)}`;
        } else if (absCover) {
            cov.src = absCover;
        }
    }
    // Always offer search. It opens the matching ebook to search; for audio-only
    // books (no linked ebook) the handler explains there's nothing to search.
    $('search-btn').classList.remove('hidden');
    applySpeed(loadSavedSpeed());
    if (!ABS_ID) { setMsg('No audiobook specified.'); return; }

    // Build the parts list from the stashed edition (multi-part) or fall back
    // to a single part = ABS_ID. Part durations come from the backend edition
    // data so we can compute book-level progress before every part has loaded.
    EDITION = loadEdition();
    if (EDITION) {
        PARTS = EDITION.parts.map(p => ({
            absId: p.absId, title: p.title, duration: +p.duration || 0,
        }));
    } else {
        PARTS = [{ absId: ABS_ID, duration: 0 }];
    }
    recomputeStarts();

    // Our saved resume position (preferred over ABS's own session currentTime),
    // reconciled with the matching ebook so reading in either format resumes
    // the other at the same spot. It's a BOOK-global time; map it onto the part
    // that contains it. An ebook winner gives a percent — converted to seconds
    // here when the book total is known, else deferred to loadPart.
    // Resume position. Prefer the record the library stashed in sessionStorage
    // (#54 / #28): it lets us paint the resumed % immediately (no 0% flash) and
    // resume without the /progress round-trip or waiting on the ABS session
    // before we know where to seek. Falls back to the network gather on a direct
    // open (no prefetch present).
    let pre = null;
    try {
        const sk = 'gr.prefetch.progress.' + ABS_ID;
        const praw = sessionStorage.getItem(sk);
        if (praw) { pre = JSON.parse(praw); sessionStorage.removeItem(sk); }
    } catch (_) {}
    if (pre && typeof pre.progress === 'number' && pre.progress > 0) {
        // Optimistic book-bar render before the audio element has loaded.
        try { $('scrubber').value = Math.round(pre.progress * 1000); fillRange($('scrubber')); } catch (_) {}
        try { $('book-pct').textContent = (pre.progress * 100).toFixed(1) + '%'; } catch (_) {}
    }

    let savedPos = 0;
    const resume = (pre && resumeShapeFromRecord(pre)) || await resolveResumePosition();
    if (resume.kind === 'seconds') {
        savedPos = resume.value;
    } else if (resume.kind === 'percent') {
        // Carry the chapter anchor (if any) for loadPart to title-match; the
        // percent is the fallback used to pick the start part and seek when the
        // chapter doesn't match.
        resumeChapter = resume.chapter || null;
        const total = bookTotal();
        if (total > 0) savedPos = resume.value * total;
        else resumePercent = resume.value;
    }
    // #207: another live instance may still be PLAYING this book right now (fold
    // ghost). Its heartbeat beats any periodic save — adopt the live position so
    // pressing play here continues from where the audio actually is.
    const live = readLiveHeartbeat();
    if (live && live.pos > savedPos) {
        savedPos = live.pos;
        resumeChapter = null;
        resumePercent = null;
    }
    let startPart = 0, localSeek = savedPos;
    if (editionDuration > 0 && savedPos > 0) {
        for (let k = 0; k < PARTS.length; k++) {
            startPart = k;
            localSeek = savedPos - partStarts[k];
            if (savedPos < partStarts[k] + partDur(k)) break;
        }
    }
    await loadPart(startPart, Math.max(0, localSeek), false);

    // Cache this book on open (#261) — all parts + the linked ebook — so the next
    // open is fully local even if we reached here by resuming straight into the
    // player (bypassing the Home-screen precache). Delayed + fire-and-forget.
    setTimeout(() => { precacheThisBook().catch(() => {}); }, 4000);
}

// Load a single part's ABS session and wire its track into the <audio>. The
// previous part's session (if any) is closed first. seekTo is part-relative;
// autoplay starts playback once the media is ready (used when auto-advancing).
async function loadPart(i, seekTo, autoplay) {
    if (i < 0 || i >= PARTS.length) return;
    if (sid && !closed) closePartSession(sid);
    curPart = i;
    setLoading();
    // Local-first (#54): if the library pre-downloaded this audiobook, play from
    // the on-device file — no ABS /play, no streaming, works in a dead zone.
    let s = await loadLocalSession(PARTS[i].absId);
    if (!s) {
        try {
            const res = await fetch(`${API_URL}/audiobooks/${encodeURIComponent(PARTS[i].absId)}/play`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            s = await res.json();
        } catch (e) {
            console.error('play failed', e);
            setMsg('Could not start playback. Is Audiobookshelf reachable?');
            return;
        }
    }
    session = s;
    sid = s.id;
    chapters = s.chapters || [];
    duration = s.duration || PARTS[i].duration || 0;
    // Backfill a part duration we didn't know up front (single-part fallback,
    // or an edition whose backend duration was missing) and re-anchor starts.
    if (!(PARTS[i].duration > 0) && duration > 0) {
        PARTS[i].duration = duration;
        recomputeStarts();
    }
    // Cross-format ebook->audio resume (#25): if the ebook stamped the chapter
    // it was in, seek to the same chapter here (matched by title) plus its
    // fraction-through, instead of the drift-prone global percent. Resolved once
    // chapters are loaded; a miss leaves the percent fallback below in place.
    // Consumed only on this initial resume load, so clear it either way.
    if (resumeChapter) {
        const cs = chapterSeekFromTitle(resumeChapter.title, resumeChapter.fraction);
        if (cs != null) { seekTo = cs; resumePercent = null; }
        resumeChapter = null;
    }
    // A cross-format ebook resume may have arrived as a percent we couldn't
    // convert until this (single-part) duration was known just above. Apply it
    // now, once, to the freshly-loaded part.
    if (resumePercent != null && !seekTo) {
        seekTo = resumePercent * bookTotal();
        resumePercent = null;
    }
    pendingSeek = seekTo || (PARTS.length === 1
        ? (s.currentTime || s.startTime || 0) : 0);

    const tracks = s.audioTracks || [];
    if (!tracks.length) { setMsg('This part has no playable tracks.'); return; }
    if (hls) { try { hls.destroy(); } catch (_) {} hls = null; }
    const hlsTrack = tracks.find(t => (t.contentUrl || '').includes('.m3u8'));
    if (hlsTrack) loadHls(hlsTrack.contentUrl);
    else loadDirect(tracks[0].contentUrl);

    renderChapters();
    setupMediaSession();
    // DON'T reveal the player here — the resume seek hasn't landed, so the bar
    // would show 0% and then jump (#28). Keep the quote loading overlay up; it's
    // hidden in applyPendingSeek once the saved position is actually applied.
    if (autoplay) { const p = audio.play(); if (p && p.catch) p.catch(() => {}); }
}

function loadDirect(url) {
    audio.src = url;
    audio.load();
}

function loadHls(url) {
    // Android WebView's <audio> can't demux HLS, so use hls.js where available.
    // Safari/iOS WebKit plays HLS natively, so fall back to a plain src there.
    if (window.Hls && Hls.isSupported()) {
        hls = new Hls({ enableWorker: true });
        hls.loadSource(url);
        hls.attachMedia(audio);
        hls.on(Hls.Events.ERROR, (_e, data) => {
            if (data && data.fatal) { console.error('hls fatal', data); setMsg('Playback stream error.'); }
        });
    } else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
        audio.src = url;
        audio.load();
    } else {
        setMsg('HLS playback is not supported on this device.');
    }
}

// Apply the resume seek once the media is ready (works for both hls.js and
// native). Runs once.
function applyPendingSeek() {
    // Re-assert the chosen speed too — some engines reset playbackRate when a
    // fresh media source finishes loading.
    if (chosenRate && audio.playbackRate !== chosenRate) audio.playbackRate = chosenRate;
    if (pendingSeek > 0 && isFinite(audio.duration)) {
        try { audio.currentTime = pendingSeek; } catch (_) {}
        pendingSeek = 0;
    }
    // Media is loaded (duration known) and any resume seek is applied — saving
    // progress is now safe.
    if (isFinite(audio.duration)) resumeApplied = true;
    // Paint the trail immediately so the scrubber shows the correct filled
    // region before the first timeupdate fires.
    updateUI();
    // Now (and only now) reveal the player — the bar shows the real resumed
    // position, never 0. Until here the quote loading overlay stayed up (#28).
    if (resumeApplied) setMsg('');
}
audio.addEventListener('loadedmetadata', applyPendingSeek);
audio.addEventListener('canplay', applyPendingSeek);

// Duration of part k: prefer the known/backfilled edition value; for the
// loaded part fall back to the live media duration.
function partDur(k) {
    if (PARTS[k] && PARTS[k].duration > 0) return PARTS[k].duration;
    if (k === curPart && isFinite(audio.duration) && audio.duration > 0) return audio.duration;
    return 0;
}
// Re-anchor each part's cumulative start and the book total from part durations.
function recomputeStarts() {
    partStarts = [];
    let acc = 0;
    for (let k = 0; k < PARTS.length; k++) { partStarts.push(acc); acc += partDur(k); }
    editionDuration = acc;
}
// Current PART duration (used by chapter math + part-relative seeking).
function partTotal() {
    return (isFinite(audio.duration) && audio.duration > 0)
        ? audio.duration : ((PARTS[curPart] && PARTS[curPart].duration) || duration || 0);
}
// Whole-BOOK duration: the summed edition length for multi-part, else the part.
function bookTotal() {
    return (PARTS.length > 1 && editionDuration > 0) ? editionDuration : partTotal();
}
// Book-global elapsed time = parts before us + position within the current part.
function globalTime() {
    return (partStarts[curPart] || 0) + (audio.currentTime || 0);
}
// Seek to a book-global time, switching parts when it lands outside this one.
function seekGlobal(g) {
    g = Math.max(0, Math.min(g, bookTotal()));
    let i = curPart;
    if (PARTS.length > 1 && editionDuration > 0) {
        i = PARTS.length - 1;
        for (let k = 0; k < PARTS.length; k++) {
            if (g < partStarts[k] + partDur(k)) { i = k; break; }
        }
    }
    const local = g - (partStarts[i] || 0);
    if (i === curPart) {
        try { audio.currentTime = Math.max(0, local); } catch (_) {}
    } else {
        loadPart(i, Math.max(0, local), !audio.paused);
    }
}

function currentChapterIndex(t) {
    let idx = -1;
    for (let i = 0; i < chapters.length; i++) {
        if (t >= (chapters[i].start || 0)) idx = i; else break;
    }
    return idx;
}

// Start/end (seconds) of the chapter containing time t. Falls back to the
// whole book when there are no chapters.
function chapterBounds(t) {
    const ci = currentChapterIndex(t);
    if (ci < 0) return { ci: -1, start: 0, end: partTotal() };
    const start = chapters[ci].start || 0;
    let end = chapters[ci].end;
    if (!(end > start)) end = (ci + 1 < chapters.length) ? (chapters[ci + 1].start || partTotal()) : partTotal();
    return { ci, start, end };
}

// Cross-format resume (#25): normalize a chapter title so ebook and audiobook
// chapter names compare equal despite punctuation/case differences. MUST stay
// identical to the reader's normChapterTitle() or matching silently fails.
function normChapterTitle(t) {
    return (t || '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ')
        .replace(/\s+/g, ' ').trim();
}

// The chapter the listener is currently in, plus how far through it (0..1).
// Stamped into the progress record on save so the ebook can resume by chapter
// instead of a drift-prone global percent. Null when there are no chapters
// (then the ebook just falls back to percent). Uses part-relative time, so the
// title is unambiguous for single-part books (the common dual-format case).
function currentChapterStamp() {
    if (!chapters.length) return null;
    const t = audio.currentTime || 0;
    const { ci, start, end } = chapterBounds(t);
    if (ci < 0) return null;
    const len = Math.max(0, end - start);
    const frac = len ? Math.max(0, Math.min(1, (t - start) / len)) : 0;
    return { title: chapters[ci].title || `Chapter ${ci + 1}`, fraction: frac };
}

// Map an ebook chapter (title + fraction-through) onto a part-relative seek time
// by matching its normalized title against this part's loaded chapters. Returns
// null when there's no confident title match — the caller then keeps the percent
// fallback. Single-part only in practice: a multi-part book only has the current
// part's chapters loaded here, so a miss cleanly defers to percent.
function chapterSeekFromTitle(title, fraction) {
    const want = normChapterTitle(title);
    if (!want || !chapters.length) return null;
    for (let i = 0; i < chapters.length; i++) {
        if (normChapterTitle(chapters[i].title) === want) {
            const start = chapters[i].start || 0;
            let end = chapters[i].end;
            if (!(end > start)) end = (i + 1 < chapters.length)
                ? (chapters[i + 1].start || partTotal()) : partTotal();
            const len = Math.max(0, end - start);
            return start + (typeof fraction === 'number' ? fraction : 0) * len;
        }
    }
    return null;
}

function updateUI() {
    // During the load window the resume seek hasn't landed yet, so currentTime
    // sits at 0. A durationchange/timeupdate here would repaint the bar to 0%
    // over the optimistic resume render, then jump when the seek applies — the
    // "shows 0, then jumps" symptom (#28). Hold the UI until the seek lands.
    if (!resumeApplied && pendingSeek > 0) return;
    const t = audio.currentTime || 0;       // part-relative (chapter math)
    const gt = globalTime();                 // book-global (book bar)
    const total = bookTotal();
    const rate = currentRate();

    // ---- Book bar (spans all parts) ----
    if (!scrubbing) { $('scrubber').value = total ? Math.round((gt / total) * 1000) : 0; fillRange($('scrubber')); }
    $('elapsed').textContent = fmt(gt);
    // Remaining is "real" wall-clock time at the current speed.
    $('remaining').textContent = '-' + fmt(Math.max(0, total - gt) / rate);
    $('book-pct').textContent = (total ? ((gt / total) * 100).toFixed(1) : '0.0') + '%';
    // #248: below the credit mark → not earning credit → flag the sync button.
    _maxPos = Math.max(_maxPos, gt);
    $('sync-btn').classList.toggle('behind', gt < _maxPos - 5);

    // ---- Chapter bar (within the current part) ----
    const { ci, start, end } = chapterBounds(t);
    const clen = Math.max(0, end - start);
    const cpos = Math.max(0, Math.min(t - start, clen));
    if (!chScrubbing) { $('ch-scrubber').value = clen ? Math.round((cpos / clen) * 1000) : 0; fillRange($('ch-scrubber')); }
    $('ch-elapsed').textContent = fmt(cpos);
    $('ch-remaining').textContent = '-' + fmt(Math.max(0, clen - cpos) / rate);
    $('ch-pct').textContent = (clen ? ((cpos / clen) * 100).toFixed(1) : '0.0') + '%';

    const ch = ci >= 0 ? chapters[ci] : null;
    let ctitle = ch ? (ch.title || `Chapter ${ci + 1}`) : '';
    if (PARTS.length > 1) ctitle = (ctitle ? ctitle + ' · ' : '') + `Part ${curPart + 1}/${PARTS.length}`;
    $('chapter-title').textContent = ctitle;
    const rows = $('chapter-list').children;
    for (let i = 0; i < rows.length; i++) rows[i].classList.toggle('active', i === ci);
    NativeMedia.state();  // throttled position push to the lock-screen scrubber
}
audio.addEventListener('timeupdate', updateUI);
audio.addEventListener('durationchange', updateUI);

// ---------- Transport controls ----------
function reflectPlayState() {
    const playing = !audio.paused && !audio.ended;
    const img = $('play-img');
    img.src = playing ? 'assets/pause.png' : 'assets/play.png';
    img.alt = playing ? 'Pause' : 'Play';
    const mini = $('ro-mini');
    if (mini) mini.textContent = playing ? '🎧 playing' : '🎧 paused';
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
    NativeMedia.state(true);  // start/refresh the foreground service + notification
}
function togglePlay() { audio.paused ? audio.play().catch(() => {}) : audio.pause(); }
function skip(delta) {
    seekGlobal(globalTime() + delta);
}
function gotoChapter(i) {
    if (i < 0) {
        // Before the first chapter of this part — go to the previous part's end.
        if (curPart > 0) loadPart(curPart - 1, Math.max(0, partDur(curPart - 1) - 1), !audio.paused);
        return;
    }
    if (i >= chapters.length) {
        // Past the last chapter of this part — advance to next part.
        if (curPart + 1 < PARTS.length) loadPart(curPart + 1, 0, !audio.paused);
        return;
    }
    audio.currentTime = chapters[i].start || 0;
    if (audio.paused) audio.play().catch(() => {});
}
function prevChapter() {
    const ci = currentChapterIndex(audio.currentTime || 0);
    // If >3s into the chapter, restart it; otherwise jump to the previous one.
    if (ci >= 0 && (audio.currentTime - (chapters[ci].start || 0)) > 3) gotoChapter(ci);
    else gotoChapter(ci - 1);
}
function nextChapter() { gotoChapter(currentChapterIndex(audio.currentTime || 0) + 1); }

$('play').addEventListener('click', togglePlay);
$('back30').addEventListener('click', () => skip(-30));
$('fwd30').addEventListener('click', () => skip(30));
$('prev-ch').addEventListener('click', prevChapter);
$('next-ch').addEventListener('click', nextChapter);
// Speed control (#265): the button (bottom-left of the controls) shows the current
// rate; tapping it reveals a vertical slider (1.0× bottom → 3.0× top). Live update
// + persist (propagates across books via localStorage). Tap away to dismiss.
$('speed').addEventListener('input', (e) => applySpeed(e.target.value));
(function () {
    const btn = $('speed-btn'), pop = $('speed-popup');
    if (!btn || !pop) return;
    btn.addEventListener('click', (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; });
    pop.addEventListener('click', (e) => e.stopPropagation());
    document.addEventListener('click', () => { if (!pop.hidden) pop.hidden = true; });
})();
// Leaving the player stops playback (close session + save final position).
$('back-btn').addEventListener('click', () => {
    // Deliberate exit — stop the "/" bootstrap bouncing back into the player
    // (#198). pagehide also clears; this is the explicit belt-and-braces path.
    if (window.ActiveBook) ActiveBook.clear();
    doClose();
    history.length > 1 ? history.back() : (location.href = 'index.html');
});

// #248: "Credit from here" — reset the word-credit high-water-mark (maxProgress /
// maxPosition) to the current spot, so forward listening credits again after a
// cross-format jump landed us ahead. Credit baseline only; no position change.
$('sync-btn').addEventListener('click', async () => {
    if (!PROGRESS_KEY) { toast('No book to sync'); return; }
    try {
        const r = await fetch(`${API_URL}/progress/${encodeURIComponent(PROGRESS_KEY)}/reset-credit-mark`, { method: 'POST' });
        if (r.ok) { _maxPos = globalTime(); $('sync-btn').classList.remove('behind'); }
        toast(r.ok ? 'Now crediting from here' : 'Sync failed');
    } catch (_) { toast('Sync failed'); }
});

// ---------- In-book reader / search overlay (dual-format works) ----------
// Hosts reader.html in an iframe so the audio in THIS document keeps playing.
const readerOverlay = $('reader-overlay');
const readerFrame = $('reader-frame');
function openReader(withSearch) {
    if (!HAS_EBOOK) { toast('No linked ebook to search for this audiobook'); return; }
    $('ro-title').textContent = TITLE;
    readerFrame.src = `reader.html?id=${encodeURIComponent(EBOOK_ID)}`
        + `&format=${encodeURIComponent(EBOOK_FORMAT || 'epub')}`
        + `&title=${encodeURIComponent(TITLE)}`;
    readerOverlay.classList.add('open');
    if (withSearch) {
        // openSearch() is a global in reader.html; same-origin lets us call it
        // once the frame's scripts are ready. Retry briefly in case the book
        // is still wiring up.
        let tries = 0;
        const tryOpen = () => {
            try { if (readerFrame.contentWindow.openSearch) { readerFrame.contentWindow.openSearch(); return; } } catch (_) {}
            if (tries++ < 12) setTimeout(tryOpen, 350);
        };
        readerFrame.onload = () => setTimeout(tryOpen, 300);
    } else {
        readerFrame.onload = null;
    }
}
function closeReader() {
    readerOverlay.classList.remove('open');
    readerFrame.onload = null;
    readerFrame.src = 'about:blank';
}
$('search-btn').addEventListener('click', () => openReader(true));
$('ro-back').addEventListener('click', closeReader);
// #208: the APK asks the page before routing back-to-Home. If the dual-format
// ebook overlay is open, close it (handled — audio unaffected); otherwise
// unhandled and the APK navigates Home (the normal player exit: saves progress
// and ends the session, same as the on-screen back button).
window.grHandleBack = function () {
    try {
        if (readerOverlay.classList.contains('open')) { closeReader(); return true; }
    } catch (_) {}
    return false;
};

audio.addEventListener('play', () => { reflectPlayState(); lastSyncAt = Date.now(); startSync(); startAutoBm(); startHb(); });
audio.addEventListener('pause', () => { reflectPlayState(); sync(); stopSync(); stopAutoBm(); stopHb(); });
audio.addEventListener('ended', () => {
    reflectPlayState(); sync();
    // Auto-advance to the next part of a multi-part edition.
    if (curPart + 1 < PARTS.length) loadPart(curPart + 1, 0, true);
});

// ---------- Scrubbers ----------
// Book scrubber — seeks across the whole book (all parts).
const scrubber = $('scrubber');
scrubber.addEventListener('input', () => { scrubbing = true;
    const total = bookTotal();
    $('elapsed').textContent = fmt((scrubber.value / 1000) * total);
    fillRange(scrubber); });
scrubber.addEventListener('change', () => {
    const total = bookTotal();
    seekGlobal((scrubber.value / 1000) * total);
    scrubbing = false;
});
// Chapter scrubber — seeks within the current chapter only.
const chScrubber = $('ch-scrubber');
chScrubber.addEventListener('input', () => { chScrubbing = true;
    const { start, end } = chapterBounds(audio.currentTime || 0);
    const clen = Math.max(0, end - start);
    $('ch-elapsed').textContent = fmt((chScrubber.value / 1000) * clen);
    fillRange(chScrubber); });
chScrubber.addEventListener('change', () => {
    const { start, end } = chapterBounds(audio.currentTime || 0);
    const clen = Math.max(0, end - start);
    audio.currentTime = start + (chScrubber.value / 1000) * clen;
    chScrubbing = false;
});

// ---------- Chapter drawer ----------
function renderChapters() {
    const list = $('chapter-list');
    list.innerHTML = '';
    chapters.forEach((ch, i) => {
        const row = document.createElement('div');
        row.className = 'ch-row';
        row.innerHTML = `<span>${(ch.title || 'Chapter ' + (i + 1)).replace(/</g, '&lt;')}</span>`
            + `<span class="ch-time">${fmt(ch.start || 0)}</span>`;
        row.addEventListener('click', () => { gotoChapter(i); closeDrawer(); });
        list.appendChild(row);
    });
}
function openDrawer() { $('drawer-backdrop').classList.add('open'); }
function closeDrawer() { $('drawer-backdrop').classList.remove('open'); }
$('chapters-btn2').addEventListener('click', openDrawer);
$('drawer-backdrop').addEventListener('click', (e) => { if (e.target === $('drawer-backdrop')) closeDrawer(); });

// ---------- Bookmarks ----------
// Bookmarks live on OUR backend keyed by bookId. The audiobook side uses
// "abs:<id>" (PROGRESS_KEY); the matching ebook (if any) uses its Calibre id.
// We store a `percent` (0..1) on every bookmark so a spot marked in one format
// surfaces in the other at the equivalent place — not exact, but close enough.
let linkedEbookId = (HAS_EBOOK && EBOOK_ID) ? EBOOK_ID : '';

// Discover the matching ebook id when the player wasn't launched with one
// (audio-only navigation of a work that does have an ebook). Fire-and-forget.
async function resolveEbookSibling() {
    if (linkedEbookId || !PROGRESS_KEY) return;
    try {
        const r = await fetch(`${API_URL}/booklinks/${encodeURIComponent(PROGRESS_KEY)}`);
        if (!r.ok) return;
        const d = await r.json();
        const s = (d.siblings || []).find(x => !String(x).startsWith('abs:'));
        if (s) linkedEbookId = String(s);
    } catch (_) {}
}

// Cross-format resume: the most-recently-updated progress record wins, whether
// it was the audiobook's own or the matching ebook's. An ebook record only
// carries a percent, so we map it onto the audio timeline. When the book total
// isn't known yet (single-part, duration arrives with the play session) we
// stash the percent in `resumePercent` for loadPart to apply post-load.
let resumePercent = null;
// Cross-format ebook->audio resume anchor (#25): {title, fraction}. Like
// resumePercent it's resolved in loadPart, once the part's chapters are loaded
// and we can title-match. Takes precedence over resumePercent when it matches.
let resumeChapter = null;
// Map a backend progress record onto the resume shape loadPart consumes.
// Shared by the network path and the sessionStorage fast path (#54 / #28).
function resumeShapeFromRecord(best) {
    if (!best || best.error || !best.updated) return null;
    if (best.mediaType === 'audiobook' && typeof best.position === 'number') {
        return { kind: 'seconds', value: best.position };
    }
    return {
        kind: 'percent',
        value: (typeof best.progress === 'number') ? best.progress : 0,
        chapter: best.chapterTitle
            ? { title: best.chapterTitle, fraction: best.chapterFraction } : null,
    };
}

async function resolveResumePosition() {
    if (!PROGRESS_KEY) return { kind: 'none' };
    // Gather every key this book's progress could live under, de-duplicated:
    //   - PROGRESS_KEY        (calibre id for dual-format, else abs:<id>)
    //   - abs:<ABS_ID>        (audio progress saved before the unified-key change;
    //                          without this, switching launch source looks like
    //                          "lost progress")
    //   - the linked ebook id (cross-format: a spot read in the ebook resumes audio)
    const keys = new Set([PROGRESS_KEY]);
    if (ABS_ID) keys.add('abs:' + ABS_ID);
    await resolveEbookSibling();
    if (linkedEbookId) keys.add(linkedEbookId);

    // Fetch all candidates in parallel (was sequential — a big part of slow resume).
    // ?as=audiobook (#256): the backend rebases a sibling ebook/physical record's
    // spot into audio-native progress through the story anchors, so the percent
    // path (progress × bookTotal) lands at the right second. Same-format audio
    // records come back verbatim.
    const recs = await Promise.all([...keys].map(async (k) => {
        try {
            const r = await fetch(`${API_URL}/progress/${encodeURIComponent(k)}?as=audiobook`);
            return r.ok ? await r.json() : null;
        } catch (_) { return null; }
    }));

    const valid = recs.filter(p => p && !p.error && p.updated);
    if (!valid.length) return { kind: 'none' };
    valid.sort((a, b) => (b.updated || 0) - (a.updated || 0));
    // Ebook winner: resumeShapeFromRecord prefers a chapter-anchored resume
    // (#25) — its chapter title is title-matched against our audio chapters once
    // loaded (loadPart), falling back to percent when there's no match.
    return resumeShapeFromRecord(valid[0]) || { kind: 'none' };
}

function curChapterTitle() {
    const ci = currentChapterIndex(audio.currentTime || 0);
    if (ci < 0) return '';
    const ch = chapters[ci];
    return ch ? (ch.title || ('Chapter ' + (ci + 1))) : '';
}

// Brief visual confirmation that a bookmark was saved (the SVG flashes pink).
function flashBookmarkAdded() {
    const b = $('bm-add');
    if (!b) return;
    b.style.color = 'var(--brand-pink)';
    setTimeout(() => { b.style.color = ''; }, 600);
}

function bookmarkBody(type) {
    const pos = globalTime();
    const total = bookTotal();
    return {
        type, bookId: PROGRESS_KEY, bookTitle: TITLE, bookAuthor: AUTHOR,
        mediaType: 'audiobook',
        position: pos, duration: total,
        percent: total ? Math.min(1, pos / total) : 0,
        text: curChapterTitle() || fmt(pos),
        chapter: curChapterTitle(),
    };
}

async function addBookmark() {
    if (!PROGRESS_KEY) return;
    flashBookmarkAdded();
    const body = bookmarkBody('bookmark');
    // Advance reading progress too — but only if this bookmark sits at (or
    // beyond) the furthest point any existing bookmark reached. A manual
    // bookmark dropped at an earlier spot (e.g. after scrubbing back) must
    // not rewind the library's progress bar.
    try {
        const existing = await fetchAllBookmarks();
        const maxPct = existing.reduce((m, n) => Math.max(m, n.percent || 0), 0);
        if ((body.percent || 0) >= maxPct) {
            saveReadingState();
            saveProgress();
        }
    } catch (_) {}
    try {
        await fetch(`${API_URL}/highlights`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    } catch (_) {}
}

// Auto-bookmark every ~60s of playback (the backend keeps only the most recent
// 10 per book, matching the ebook behaviour). Driven by an interval that only
// runs while audio is playing (started/stopped alongside the progress sync).
let autoBmTimer = null;
function startAutoBm() { if (!autoBmTimer) autoBmTimer = setInterval(createAutoBookmark, 60000); }
function stopAutoBm() { if (autoBmTimer) { clearInterval(autoBmTimer); autoBmTimer = null; } }
async function createAutoBookmark() {
    if (!PROGRESS_KEY || !bookTotal()) return;
    // Mirror position to localStorage + backend immediately so the library
    // shows fresh progress at every auto-bookmark interval (every ~60s).
    saveReadingState();
    saveProgress();
    try {
        await fetch(`${API_URL}/highlights`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(bookmarkBody('auto-bookmark')),
        });
    } catch (_) {}
}

// Normalise a stored bookmark to a common shape with a percent + (optional)
// absolute audio position. ebook bookmarks fall back to page/total for percent.
function normBookmark(b, origin) {
    let percent = (typeof b.percent === 'number') ? b.percent : null;
    if (percent == null && origin === 'ebook' && b.total) percent = (b.page || 0) / b.total;
    const pos = (typeof b.position === 'number') ? b.position : null;
    return { id: b.id, origin, type: b.type, percent, pos,
             text: b.text || '', chapter: b.chapter || '', note: b.note || '', created: b.created || 0 };
}

async function fetchAllBookmarks() {
    // De-dup the keys first: for a dual-format book PROGRESS_KEY === linkedEbookId
    // (both the Calibre id), so also fetching linkedEbookId would return — and
    // render — every bookmark twice (#260). Collapse to a Set, then dedupe the
    // collected records by id defensively.
    const keys = new Set([PROGRESS_KEY]);
    if (linkedEbookId) keys.add(linkedEbookId);
    const reqs = [];
    for (const k of keys) {
        reqs.push(fetch(`${API_URL}/highlights?bookId=${encodeURIComponent(k)}&type=bookmark`));
        reqs.push(fetch(`${API_URL}/highlights?bookId=${encodeURIComponent(k)}&type=auto-bookmark`));
    }
    const out = [];
    const seen = new Set();
    try {
        const res = await Promise.all(reqs);
        for (let i = 0; i < res.length; i++) {
            if (!res[i].ok) continue;
            const data = await res[i].json();
            for (const b of (data.items || [])) {
                // Audio view: only audio-origin bookmarks (#80). mediaType is the
                // reliable discriminant — also catches audio bookmarks cross-keyed
                // under the linked ebook id; ebook bookmarks (mediaType ''/absent) drop.
                if (b.mediaType !== 'audiobook') continue;
                if (b.id != null) { if (seen.has(b.id)) continue; seen.add(b.id); }
                out.push(normBookmark(b, 'audio'));
            }
        }
    } catch (_) {}
    // Newest / furthest-progress first.
    out.sort((a, b) => (b.percent || 0) - (a.percent || 0));
    return out;
}

function seekToBookmark(n) {
    const total = bookTotal();
    const g = (n.origin === 'audio' && n.pos != null) ? n.pos : (n.percent || 0) * total;
    seekGlobal(g);
    if (audio.paused) audio.play().catch(() => {});
    closeBookmarks();
}

async function deleteBookmark(id) {
    try { await fetch(`${API_URL}/highlights/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
    catch (_) {}
    renderBookmarks();
}

// ── Bookmark notes (#259) ────────────────────────────────────────────────
// Long-press (touch, ~500ms) or right-click (mouse) a bookmark to edit its
// note. Long-press cancels on scroll/drag and swallows the trailing click so
// it doesn't also seek.
function attachNoteGesture(el, handler) {
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); handler(); });
    let t = null, startY = 0, fired = false;
    const clear = () => { if (t) { clearTimeout(t); t = null; } };
    el.addEventListener('touchstart', (e) => {
        fired = false;
        startY = (e.touches && e.touches[0]) ? e.touches[0].clientY : 0;
        clear();
        t = setTimeout(() => { fired = true; handler(); }, 500);
    }, { passive: true });
    el.addEventListener('touchmove', (e) => {
        const y = (e.touches && e.touches[0]) ? e.touches[0].clientY : 0;
        if (Math.abs(y - startY) > 10) clear();
    }, { passive: true });
    el.addEventListener('touchend', clear, { passive: true });
    el.addEventListener('touchcancel', clear, { passive: true });
    el.addEventListener('click', (e) => {
        if (fired) { e.stopPropagation(); e.preventDefault(); fired = false; }
    }, true);
}

// App-styled note editor. `initial` prefills; Save with empty clears the note.
function openNoteEditor(initial, onSave) {
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:11000;display:flex;align-items:center;justify-content:center;padding:24px;';
    const card = document.createElement('div');
    card.style.cssText = 'background:#fff;border-radius:14px;width:100%;max-width:420px;padding:18px;box-shadow:0 10px 40px rgba(0,0,0,0.35);';
    card.innerHTML = '<div style="font-size:16px;font-weight:600;color:#212529;margin-bottom:10px;">Bookmark note</div>';
    const ta = document.createElement('textarea');
    ta.value = initial || '';
    ta.placeholder = 'Add a description…';
    ta.style.cssText = 'width:100%;min-height:96px;border:1px solid #ced4da;border-radius:8px;padding:10px;font-size:15px;line-height:1.4;color:#212529;resize:vertical;box-sizing:border-box;';
    card.appendChild(ta);
    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;margin-top:14px;';
    const cancel = document.createElement('button');
    cancel.textContent = 'Cancel';
    cancel.style.cssText = 'background:#f1f3f5;color:#495057;border:1px solid #e9ecef;padding:9px 16px;border-radius:8px;font-size:15px;cursor:pointer;';
    const save = document.createElement('button');
    save.textContent = 'Save';
    save.style.cssText = 'background:#FF6600;color:#fff;border:none;padding:9px 18px;border-radius:8px;font-size:15px;cursor:pointer;';
    const close = () => ov.remove();
    cancel.addEventListener('click', close);
    save.addEventListener('click', () => { const v = ta.value.trim(); close(); onSave(v); });
    // Ctrl/Cmd+Enter saves (#259) — Esc cancels.
    ta.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); save.click(); }
        else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
    btns.appendChild(cancel); btns.appendChild(save);
    card.appendChild(btns);
    ov.appendChild(card);
    document.body.appendChild(ov);
    setTimeout(() => { ta.focus(); }, 50);
}

async function saveBookmarkNote(id, note) {
    try {
        const r = await fetch(`${API_URL}/highlights/${encodeURIComponent(id)}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ note })
        });
        return r.ok;
    } catch (_) { return false; }
}

async function renderBookmarks() {
    const list = $('bm-list');
    list.innerHTML = '<div class="bm-empty">Loading…</div>';
    const items = await fetchAllBookmarks();
    list.innerHTML = '';
    if (!items.length) {
        list.innerHTML = '<div class="bm-empty">No bookmarks yet.<br>Tap the bookmark button to save your spot.</div>';
        return;
    }
    // Same format badge SVGs as the book-cover badges in index.html, scaled to 13 px.
    const ICON_EBOOK = '<svg width="13" height="13" viewBox="0 0 11 11" xmlns="http://www.w3.org/2000/svg"><rect width="11" height="11" rx="2" fill="#0066CC"/><rect x="2" y="2.5" width="7" height="1.2" rx="0.6" fill="white"/><rect x="2" y="4.9" width="7" height="1.2" rx="0.6" fill="white"/><rect x="2" y="7.3" width="5" height="1.2" rx="0.6" fill="white"/></svg>';
    const ICON_AUDIO = '<svg width="13" height="13" viewBox="0 0 11 11" xmlns="http://www.w3.org/2000/svg"><rect width="11" height="11" rx="2" fill="#FF6600"/><path d="M1.8 7 A3.7 4.5 0 0 1 9.2 7" stroke="white" stroke-width="1.3" fill="none" stroke-linecap="round"/><rect x="1.2" y="6.5" width="1.5" height="3" rx="0.5" fill="white"/><rect x="8.3" y="6.5" width="1.5" height="3" rx="0.5" fill="white"/></svg>';
    // Manual bookmarks: neutral dark icon. Auto-saved: brand-purple tint.
    // (Light surface now — black glyph reads directly; no invert.)
    const BM_ICON_MANUAL = '<img src="assets/bookmark.png" style="width:13px;height:13px;opacity:0.7;vertical-align:middle;margin-right:5px;" aria-hidden="true">';
    const BM_ICON_AUTO   = '<img src="assets/bookmark.png" style="width:13px;height:13px;filter:invert(28%) sepia(83%) saturate(1700%) hue-rotate(258deg) brightness(90%);vertical-align:middle;margin-right:5px;" aria-hidden="true">';

    // One furthest-first list — manual + auto merged (already sorted % desc), no
    // section split, one compact row each (#80).
    const total = bookTotal();
    for (const n of items) {
        const bmIcon = (n.type === 'auto-bookmark') ? BM_ICON_AUTO : BM_ICON_MANUAL;
        const row = document.createElement('div');
        row.className = 'bm-row';
        const main = document.createElement('div');
        main.className = 'bm-main';
        const label = document.createElement('div');
        label.className = 'bm-label';
        const txt = (n.chapter || n.text || 'Bookmark').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        label.innerHTML = bmIcon + txt;
        const sub = document.createElement('div');
        sub.className = 'bm-sub';
        const pctTxt = ((n.percent || 0) * 100).toFixed(1) + '%';
        const timeTxt = (n.pos != null) ? fmt(n.pos) : fmt((n.percent || 0) * total);
        sub.innerHTML = pctTxt + ' · ' + timeTxt;
        main.appendChild(label); main.appendChild(sub);
        // Optional user note (#259) — third line, brand-orange with a 📝.
        const noteTxt = (n.note && String(n.note).trim()) ? String(n.note).trim() : '';
        if (noteTxt) {
            const note = document.createElement('div');
            note.className = 'bm-note';
            note.style.cssText = 'font-size:12px;color:#FF6600;margin-top:3px;line-height:1.35;white-space:pre-wrap;';
            note.textContent = '📝 ' + noteTxt;
            main.appendChild(note);
        }
        const del = document.createElement('button');
        del.className = 'bm-del'; del.textContent = '×'; del.title = 'Delete';
        del.addEventListener('click', (e) => { e.stopPropagation(); deleteBookmark(n.id); });
        row.appendChild(main); row.appendChild(del);
        row.addEventListener('click', () => seekToBookmark(n));
        // Long-press (touch) / right-click (mouse) → edit the note (#259).
        attachNoteGesture(row, () => openNoteEditor(n.note || '', async (v) => {
            if (await saveBookmarkNote(n.id, v)) { n.note = v; renderBookmarks(); }
        }));
        list.appendChild(row);
    }
}

function openBookmarks() { $('bm-backdrop').classList.add('open'); renderBookmarks(); }
function closeBookmarks() { $('bm-backdrop').classList.remove('open'); }
$('bm-add').addEventListener('click', addBookmark);
$('bm-list-btn').addEventListener('click', openBookmarks);
$('bm-backdrop').addEventListener('click', (e) => { if (e.target === $('bm-backdrop')) closeBookmarks(); });

// ---------- Reading session (#57) ----------
// A session id ties every progress save from one listening sitting together so
// the backend can build per-book session stats. Re-opening the same audiobook
// within the coalesce window continues the same session. Keyed per book+format.
const SESSION_COALESCE_MS = 10 * 60 * 1000; // 10 min
const SESSION_LS_KEY = 'gr.session.audio.' + (PROGRESS_KEY || ABS_ID);
let _session = null;
function _sessionUUID() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (_) {}
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}
function initSession() {
    const now = Date.now();
    let prev = null;
    try { prev = JSON.parse(localStorage.getItem(SESSION_LS_KEY) || 'null'); } catch (_) {}
    if (prev && prev.id && (now - (prev.last || 0)) < SESSION_COALESCE_MS) {
        _session = { id: prev.id, start: prev.start || now };
    } else {
        _session = { id: _sessionUUID(), start: now };
    }
    persistSession();
}
function persistSession() {
    if (!_session || !PROGRESS_KEY) return;
    try {
        localStorage.setItem(SESSION_LS_KEY, JSON.stringify({
            id: _session.id, start: _session.start, last: Date.now()
        }));
    } catch (_) {}
}

// ---------- Progress sync ----------
// Persist book-global resume position to localStorage (for instant library
// updates) AND our backend (keyed by abs:<id>) so it survives across
// sessions/devices and shows up in the "Continue reading" list.
function saveReadingState() {
    if (!PROGRESS_KEY) return;
    const pos = globalTime();
    const total = bookTotal();
    try {
        localStorage.setItem('ereader.state.' + PROGRESS_KEY, JSON.stringify({
            position: pos,
            duration: total,
            progress: total ? Math.min(1, pos / total) : 0,
            mediaType: 'audiobook',
            ts: Date.now()
        }));
    } catch (_) {}
}

function saveProgress() {
    // Keep the active-book marker fresh during long locked-screen listening so
    // the "/" bootstrap's staleness check still passes after hours (#198).
    if (window.ActiveBook) ActiveBook.set();
    // Don't persist until the resume seek has landed — otherwise a save fired
    // during the load window (pause/leave/sync) writes ~0 over the real spot.
    if (!PROGRESS_KEY || !resumeApplied) return;
    if (!_session) initSession();
    saveReadingState();
    const pos = globalTime();
    const total = bookTotal();
    const stamp = currentChapterStamp();   // #25 cross-format resume anchor
    fetch(`${API_URL}/progress/${encodeURIComponent(PROGRESS_KEY)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            bookTitle: TITLE, bookAuthor: AUTHOR, mediaType: 'audiobook',
            absId: ABS_ID, format: 'audiobook',
            position: pos, duration: total,
            // Current playback speed so the backend logs real listening time
            // (content advanced ÷ rate), not nominal audiobook duration. (#46)
            playbackRate: currentRate(),
            progress: total ? Math.min(1, pos / total) : 0,
            chapterTitle: stamp ? stamp.title : undefined,
            chapterFraction: stamp ? stamp.fraction : undefined,
            // Per-session event log (#57): id + start of the current sitting.
            sessionId:    _session ? _session.id : undefined,
            sessionStart: _session ? _session.start : undefined,
        }),
        keepalive: true,
    }).catch(() => {});
    persistSession();   // keep the coalesce timestamp fresh
}

// timeListened is a DELTA (seconds since the previous sync), not cumulative.
// ABS sync is always part-relative (ABS knows nothing about our edition model).
function sync() {
    if (!sid || closed) return;
    const now = Date.now();
    const delta = lastSyncAt ? (now - lastSyncAt) / 1000 : 0;
    lastSyncAt = now;
    fetch(`${API_URL}/audiobooks/sessions/${sid}/sync`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ currentTime: audio.currentTime || 0,
                               timeListened: Math.max(0, delta), duration: partTotal() }),
        keepalive: true,
    }).catch(() => {});
    saveProgress();
}
function startSync() { if (!syncTimer) syncTimer = setInterval(sync, 15000); }
function stopSync() { if (syncTimer) { clearInterval(syncTimer); syncTimer = null; } }

// Close a single part's ABS session (best-effort; called when switching parts
// mid-playback or when the user leaves the player entirely).
function closePartSession(sessId) {
    if (!sessId) return;
    const body = JSON.stringify({ currentTime: audio.currentTime || 0, duration: partTotal() });
    try {
        navigator.sendBeacon(`${API_URL}/audiobooks/sessions/${sessId}/close`,
            new Blob([body], { type: 'application/json' }));
    } catch (_) {
        fetch(`${API_URL}/audiobooks/sessions/${sessId}/close`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body, keepalive: true }).catch(() => {});
    }
}

function doClose() {
    if (closed) return;
    closed = true;
    stopSync();
    stopAutoBm();
    stopHb();   // #207: a really-exiting player must not leave a 'playing' heartbeat
    saveProgress();
    closePartSession(sid);
    NativeMedia.stop();  // tear down the foreground service + notification
}
window.addEventListener('pagehide', doClose);
window.addEventListener('beforeunload', doClose);

// ---------- Lock-screen / notification controls ----------
function setupMediaSession() {
    if (!('mediaSession' in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
        title: TITLE, artist: AUTHOR, album: AUTHOR,
        artwork: ABS_ID ? [{ src: `${API_URL}/audiobooks/${encodeURIComponent(ABS_ID)}/cover`, sizes: '512x512', type: 'image/jpeg' }] : [],
    });
    const h = navigator.mediaSession.setActionHandler.bind(navigator.mediaSession);
    try {
        h('play', () => audio.play());
        h('pause', () => audio.pause());
        h('seekbackward', () => skip(-30));
        h('seekforward', () => skip(30));
        h('previoustrack', prevChapter);
        h('nexttrack', nextChapter);
    } catch (_) {}
}

// ---------- Native (Android) media session bridge ----------
// Inside the APK WebView, window.Android exposes mediaStart/mediaState/mediaStop
// (MainActivity.JsBridge). These drive the foreground PlaybackService that keeps
// audio alive when the screen locks and routes hardware/headphone/lock-screen
// media buttons back here via window.__mediaControl. No-ops in a desktop browser.
const NativeMedia = (() => {
    const has = typeof Android !== 'undefined' && Android
        && typeof Android.mediaStart === 'function';
    let started = false, lastPush = 0;
    return {
        start() {
            if (!has) return;
            try {
                Android.mediaStart(TITLE, AUTHOR,
                    ABS_ID ? `${API_URL}/audiobooks/${encodeURIComponent(ABS_ID)}/cover` : '');
                started = true;
            } catch (_) {}
        },
        // force=true pushes immediately (play/pause change); otherwise throttled
        // so timeupdate (~4x/s) doesn't spam the service with position updates.
        state(force) {
            const now = Date.now();
            if (!force && now - lastPush < 2000) return;
            lastPush = now;
            saveReadingState();
            if (!has) return;
            if (!started) this.start();
            try {
                Android.mediaState(!audio.paused && !audio.ended,
                    globalTime(), bookTotal(), chosenRate);
            } catch (_) {}
        },
        stop() {
            if (!has || !started) return;
            started = false;
            try { Android.mediaStop(); } catch (_) {}
        },
    };
})();

// Media-button / notification actions from the native MediaSession land here.
// Vocabulary mirrors PlaybackService's MediaSession callback (play / pause /
// next / prev / forward / backward / seek:<ms>).
window.__mediaControl = function (action) {
    if (!action) return;
    if (action === 'play') audio.play().catch(() => {});
    else if (action === 'pause') audio.pause();
    else if (action === 'next') nextChapter();
    else if (action === 'prev') prevChapter();
    else if (action === 'forward') skip(30);
    else if (action === 'backward') skip(-30);
    else if (action.indexOf('seek:') === 0) {
        const ms = parseFloat(action.slice(5));
        if (!isNaN(ms)) seekGlobal(ms / 1000);
    }
};

init();
