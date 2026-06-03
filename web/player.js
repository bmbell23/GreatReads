// GreatReads audiobook player. Talks to the Flask backend's ABS session proxy:
//   POST /api/audiobooks/<absId>/play        -> start session, get track URLs
//   POST /api/audiobooks/sessions/<sid>/sync  -> report progress (delta seconds)
//   POST /api/audiobooks/sessions/<sid>/close -> end session
// The proxy hands back absolute, token-bearing media URLs; we never see the
// ABS token. HLS (transcode) is played via hls.js; single-file direct play
// feeds the <audio> element straight.
const API_URL = 'http://100.69.184.113:8091/api';
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

// We persist resume position to our own backend (keyed by the ABS id) so it
// survives sessions/devices and surfaces in the library "Continue reading"
// list, exactly like ebook progress. Speed is a global user preference.
const PROGRESS_KEY = ABS_ID ? ('abs:' + ABS_ID) : '';
const SPEED_KEY = 'ereader.audio.speed';

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

// Session state (always reflects the CURRENTLY-loaded part).
let session = null, sid = null, chapters = [], duration = 0;
let hls = null, lastSyncAt = 0, syncTimer = null, pendingSeek = 0;
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
    if (!text) { el.classList.add('hidden'); return; }
    el.textContent = text;
    el.classList.remove('hidden');
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
    $('speed').value = r;
    $('speed-pill').textContent = r.toFixed(1) + '×';
    fillRange($('speed'));
    try { localStorage.setItem(SPEED_KEY, String(r)); } catch (_) {}
    updateUI();
}

async function init() {
    $('title').textContent = TITLE;
    $('author').textContent = AUTHOR;
    $('now-playing').textContent = TITLE;
    if (ABS_ID) $('cover').src = `${API_URL}/audiobooks/${encodeURIComponent(ABS_ID)}/cover`;
    if (HAS_EBOOK) $('search-btn').classList.remove('hidden');
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

    // Our saved resume position (preferred over ABS's own session currentTime).
    // It's a BOOK-global time; map it onto the part that contains it.
    let savedPos = 0;
    if (PROGRESS_KEY) {
        try {
            const r = await fetch(`${API_URL}/progress/${encodeURIComponent(PROGRESS_KEY)}`);
            if (r.ok) {
                const p = await r.json();
                if (p && typeof p.position === 'number') savedPos = p.position;
            }
        } catch (_) {}
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
}

// Load a single part's ABS session and wire its track into the <audio>. The
// previous part's session (if any) is closed first. seekTo is part-relative;
// autoplay starts playback once the media is ready (used when auto-advancing).
async function loadPart(i, seekTo, autoplay) {
    if (i < 0 || i >= PARTS.length) return;
    if (sid && !closed) closePartSession(sid);
    curPart = i;
    setMsg('Loading…');
    let s;
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
    setMsg('');
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
    // Paint the trail immediately so the scrubber shows the correct filled
    // region before the first timeupdate fires.
    updateUI();
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

function updateUI() {
    const t = audio.currentTime || 0;       // part-relative (chapter math)
    const gt = globalTime();                 // book-global (book bar)
    const total = bookTotal();
    const rate = currentRate();

    // ---- Book bar (spans all parts) ----
    if (!scrubbing) { $('scrubber').value = total ? Math.round((gt / total) * 1000) : 0; fillRange($('scrubber')); }
    $('elapsed').textContent = fmt(gt);
    // Remaining is "real" wall-clock time at the current speed.
    $('remaining').textContent = '-' + fmt(Math.max(0, total - gt) / rate);
    $('book-pct').textContent = (total ? Math.round((gt / total) * 100) : 0) + '%';

    // ---- Chapter bar (within the current part) ----
    const { ci, start, end } = chapterBounds(t);
    const clen = Math.max(0, end - start);
    const cpos = Math.max(0, Math.min(t - start, clen));
    if (!chScrubbing) { $('ch-scrubber').value = clen ? Math.round((cpos / clen) * 1000) : 0; fillRange($('ch-scrubber')); }
    $('ch-elapsed').textContent = fmt(cpos);
    $('ch-remaining').textContent = '-' + fmt(Math.max(0, clen - cpos) / rate);
    $('ch-pct').textContent = (clen ? Math.round((cpos / clen) * 100) : 0) + '%';

    const ch = ci >= 0 ? chapters[ci] : null;
    let ctitle = ch ? (ch.title || `Chapter ${ci + 1}`) : '';
    if (PARTS.length > 1) ctitle = (ctitle ? ctitle + ' · ' : '') + `Part ${curPart + 1}/${PARTS.length}`;
    $('chapter-title').textContent = ctitle;
    const rows = $('chapter-list').children;
    for (let i = 0; i < rows.length; i++) rows[i].classList.toggle('active', i === ci);
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
// Speed slider — live update + persist (propagates across books via localStorage).
$('speed').addEventListener('input', (e) => applySpeed(e.target.value));
// Leaving the player stops playback (close session + save final position).
$('back-btn').addEventListener('click', () => { doClose(); history.length > 1 ? history.back() : (location.href = 'index.html'); });

// ---------- In-book reader / search overlay (dual-format works) ----------
// Hosts reader.html in an iframe so the audio in THIS document keeps playing.
const readerOverlay = $('reader-overlay');
const readerFrame = $('reader-frame');
function openReader(withSearch) {
    if (!HAS_EBOOK) return;
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

audio.addEventListener('play', () => { reflectPlayState(); lastSyncAt = Date.now(); startSync(); });
audio.addEventListener('pause', () => { reflectPlayState(); sync(); stopSync(); });
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

// ---------- Progress sync ----------
// Persist book-global resume position to OUR backend (keyed by abs:<id>) so
// it survives across sessions/devices and shows up in the library list.
function saveProgress() {
    if (!PROGRESS_KEY) return;
    const pos = globalTime();
    const total = bookTotal();
    fetch(`${API_URL}/progress/${encodeURIComponent(PROGRESS_KEY)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            bookTitle: TITLE, bookAuthor: AUTHOR, mediaType: 'audiobook',
            absId: ABS_ID, format: 'audiobook',
            position: pos, duration: total,
            progress: total ? Math.min(1, pos / total) : 0,
        }),
        keepalive: true,
    }).catch(() => {});
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
    saveProgress();
    closePartSession(sid);
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

init();
