/* Check Libby + Libby details/borrow popup — shared across pages (#205).
 *
 * Extracted from books.html so the shared cover-tap popup's "Check Libby" works
 * IN PLACE on every page (Library, Home, TBR, Journal, series/author strips)
 * instead of redirecting to the Store. Everything here is page-agnostic: it
 * talks to /api/libby/* and renders through GreatReads.openBookActions.
 * Store-page-only hooks (search-shelf re-render, the Libby settings modal, the
 * Newly-Imported badge) are typeof-guarded.
 *
 * Loads after app.js (needs GreatReads.apiCall / openBookActions / showToast).
 */

// Shared helpers (the Store page declares its own identical copies later, which
// harmlessly override these on that page).
if (typeof window.esc !== 'function')
    window.esc = s => (s || '').replace(/[&<>"]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
if (typeof window.dispTitle !== 'function')
    window.dispTitle = t => (t && String(t).trim()) ? t : 'Untitled';

// The engine appends a `libraries` entry per matching seed query, so the same
// library repeats. Collapse to one row per library, keeping the best availability
// (available first, then shortest wait).
function libbyDedupeLibraries(libs) {
    const byKey = new Map();
    for (const l of (libs || [])) {
        const k = l.key || l.cardId;
        const cur = byKey.get(k);
        const better = !cur
            || (l.isAvailable && !cur.isAvailable)
            || (!cur.isAvailable && !l.isAvailable && (l.estimatedWaitDays || 9999) < (cur.estimatedWaitDays || 9999));
        if (better) byKey.set(k, l);
    }
    return [...byKey.values()];
}

// Map an engine search row → a Books card shape (kind:'libby'). A UNIFIED row (#197)
// carries `formats:{ebook,audiobook}` (each with its own OverDrive titleId + per-format
// availability/libraries); a single-media row is flat (backward compatible).
function _libbyFmt(f) {
    return f ? {
        titleId: String(f.titleId),
        isAvailable: !!f.isAvailable,
        onHold: !!f.onHold,
        holdsCount: f.holdsCount,
        estimatedWaitDays: f.estimatedWaitDays,
        libraries: libbyDedupeLibraries(f.libraries),
    } : null;
}
function libbyRowToCard(row) {
    const rf = row.formats || null;
    let formats, complete;
    if (rf) {
        // Unified search knows BOTH formats' status (present or definitively absent).
        formats = { ebook: _libbyFmt(rf.ebook), audiobook: _libbyFmt(rf.audiobook) };
        complete = true;
    } else {
        // Single-media row (series strip = ebook, or a media=X search): only ONE format
        // is known — don't imply anything about the other.
        const media = row.type || 'ebook';
        formats = { ebook: null, audiobook: null };
        formats[media] = _libbyFmt({
            titleId: row.id, isAvailable: row.isAvailable, onHold: row.onHold,
            holdsCount: row.holdsCount, estimatedWaitDays: row.estimatedWaitDays, libraries: row.libraries,
        });
        complete = false;
    }
    const eb = formats.ebook, ab = formats.audiobook;
    const primary = eb || ab;   // for the title_id used by details/cover/series lookups
    return {
        kind: 'libby',
        title_id: String((primary && primary.titleId) || row.id),
        formats,
        formatsComplete: complete,
        mediaType: row.type || (eb ? 'ebook' : ab ? 'audiobook' : 'ebook'),
        title: row.title,
        author: row.author,
        series: row.series || '',
        series_number: (row.seriesIndex !== '' && row.seriesIndex != null) ? row.seriesIndex : null,
        cover_url: row.cover || '',
        status: 'unowned',
        isAvailable: !!row.isAvailable,
        onHold: !!row.onHold,
        inLibrary: !!row.inLibrary,
        holdsCount: row.holdsCount != null ? row.holdsCount : (primary && primary.holdsCount),
        estimatedWaitDays: row.estimatedWaitDays != null ? row.estimatedWaitDays : (primary && primary.estimatedWaitDays),
        // Backward-compat top-level libraries (single-media rows / series items); unified
        // borrow uses the per-format libraries instead.
        libraries: libbyDedupeLibraries(row.libraries || (primary && primary.libraries) || []),
        _row: row,
    };
}

// Check Libby (#154): from a Wishlist/New/Upcoming book popup, search Libby for this
// title+author, pick the best match, and open the existing Borrow/Hold panel. Wired
// into the shared popup via grOpenBookActions (app.js) for non-owned books.
function grPickLibbyMatch(rows, title) {
    if (!rows || !rows.length) return null;
    const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean);
    const want = new Set(norm(title));
    if (!want.size) return rows[0];
    let best = null, bestScore = 0;
    for (const row of rows) {
        const got = new Set(norm(row.title));
        let overlap = 0; want.forEach(w => { if (got.has(w)) overlap++; });
        const score = overlap / want.size;
        if (score > bestScore) { bestScore = score; best = row; }
    }
    return bestScore >= 0.6 ? best : null;   // guard against a wrong-title top hit
}

async function grCheckLibby(titleEnc, authorEnc) {
    const title = decodeURIComponent(titleEnc || ''), author = decodeURIComponent(authorEnc || '');
    if (!title) return;
    GreatReads.showToast('Searching Libby…', 'info');
    let r;
    try {
        r = await GreatReads.apiCall(`/libby/search?q=${encodeURIComponent((title + ' ' + author).trim())}&library=all&page=1&per_page=10`);
    } catch (e) { GreatReads.showToast('Libby search failed', 'danger'); return; }
    const rows = (r && Array.isArray(r.results)) ? r.results : [];
    const match = grPickLibbyMatch(rows, title);
    if (!match) { GreatReads.showToast('No Libby match found for this title.', 'warning'); return; }
    const card = libbyRowToCard(match);
    try { await annotateLibbyOwnership([card]); } catch (e) { /* best-effort */ }
    openLibbyDetails(card);
}

// Annotate rendered Libby cards with real GreatReads ownership (title+author match
// against our library), then re-render so owned titles show the check + steer the
// popup away from a redundant borrow.
async function annotateLibbyOwnership(list) {
    const libs = (list || []).filter(c => c.kind === 'libby');
    if (!libs.length) return;
    let r;
    try { r = await GreatReads.apiCall('/libby/ownership', { method: 'POST', data: { items: libs.map(c => ({ title: c.title, author: c.author })) } }); }
    catch (e) { return; }
    const res = (r && r.results) || [];
    libs.forEach((c, i) => { const o = res[i]; if (o) { c.grOwned = o.owned; c.grBookId = o.book_id; c.calibreId = o.calibre_id; } });
    // Store-page hook: re-render the Libby search shelf if it's showing (other
    // pages have no shelf — the popup reads grOwned directly).
    if (typeof shelfStatus !== 'undefined' && shelfStatus === 'libby' && typeof renderLibby === 'function')
        renderLibby(document.getElementById('searchInput').value.trim());
}

// The Libby result currently open in the shared popup (borrow/hold handlers read it).
let libbyActive = null;

// True when GreatReads already owns this title (server-annotated grOwned wins;
// falls back to the engine's Calibre fuzzy match).
// "In your GreatReads library" = ONLY the real title+author match (grOwned, set by
// annotateLibbyOwnership). Do NOT fall back to Libby's own `inLibrary` flag — that means
// "in the OverDrive collection", not that YOU own it, and reading it before the async
// match finished caused false "In library" that flickered off (same-author lookalikes).
const libbyIsOwned = c => !!c.grOwned;

function libbyWaitText(days) {
    if (days == null || days <= 0) return 'wait';
    return `~${days >= 14 ? Math.round(days / 7) + 'w' : days + 'd'} wait`;
}

// Library chooser — one row per credentialed library, available first, each showing
// availability so you can SEE (and pick) where you're borrowing from (item #7).
function libbyLibraryChooser(c) {
    const libs = (c.libraries || []).slice().sort((a, b) => (b.isAvailable ? 1 : 0) - (a.isAvailable ? 1 : 0));
    if (!libs.length) {
        return `<div class="small text-muted">No credentialed library offers this title yet. Add a card's website credentials in the ${typeof openLibby === 'function' ? '<a href="#" onclick="openLibby();return false;">Libby panel</a>' : 'Libby panel (Store page)'}.</div>`;
    }
    const opts = libs.map(l => {
        const hint = l.isAvailable ? 'available now' : libbyWaitText(l.estimatedWaitDays);
        return `<option value="${esc(String(l.cardId))}" data-avail="${l.isAvailable ? 1 : 0}">${esc((l.key || '').toUpperCase())} — ${hint}</option>`;
    }).join('');
    const availCount = libs.filter(l => l.isAvailable).length;
    const summary = availCount
        ? `<span class="text-success">Available now at ${availCount} of ${libs.length} ${libs.length === 1 ? 'library' : 'libraries'}.</span>`
        : `<span class="text-warning">Not available now — you can place a hold.</span>`;
    return `<div class="small mb-1">${summary}</div>
        <label class="form-label small mb-1">Borrow from</label>
        <select id="libbyCardSel" class="form-select form-select-sm mb-2" onchange="libbyRenderAction()">${opts}</select>`;
}

// Render the Borrow/Hold action based on the SELECTED library's availability (item #2)
// and GreatReads ownership (item #6). Called on open + whenever the library changes.
// Pick the card to borrow a given format from: first available library, else first.
function _libbyBestCard(fmt) {
    const libs = (fmt && fmt.libraries) || [];
    const pick = libs.find(l => l.isAvailable) || libs[0];
    return pick ? String(pick.cardId) : '';
}
// Unified 'one book, two formats' actions (#197): a row per available format, each
// with its own Borrow & Download (ebook → .acsm path; audiobook → v-chip Listen).
// App-wide format convention (matches journal/stats): audiobook = orange headphones,
// ebook = blue book. Keep these in one place so it's consistent everywhere.
const LIBBY_FORMAT_SPECS = [
    { media: 'ebook', label: 'Ebook', icon: 'fa-book', color: '#0d6efd', fn: 'libbyBorrow' },
    { media: 'audiobook', label: 'Audiobook', icon: 'fa-headphones', color: '#FF6600', fn: 'libbyBorrowAudiobook' },
];
function libbyFormatsHtml(c) {
    const f = c.formats || {};
    const owned = libbyIsOwned(c);
    const rows = [];
    for (const s of LIBBY_FORMAT_SPECS) {
        const fmt = f[s.media];
        const label = `<i class="fas ${s.icon} me-2" style="color:${s.color};"></i><strong>${s.label}</strong>`;
        if (!fmt) {
            // Only assert "not on Libby" when the search actually checked this format
            // (a unified search). For a single-media/series result, stay silent.
            if (c.formatsComplete) {
                rows.push(`<div class="d-flex align-items-center justify-content-between border rounded px-2 py-1 mb-1 opacity-50">
                    <span>${label} · <span class="text-muted small">not on Libby</span></span></div>`);
            }
            continue;
        }
        const card = _libbyBestCard(fmt);
        let action, avail;
        if (fmt.onHold) {
            action = '<span class="badge bg-info text-dark"><i class="fas fa-clock me-1"></i>On hold</span>';
            avail = '<span class="text-info small">On hold</span>';
        } else if (fmt.isAvailable) {
            const cls = owned ? 'btn-outline-secondary' : 'btn-primary';
            action = `<button class="btn btn-sm ${cls}" onclick="${s.fn}('${fmt.titleId}','${card}',this)"><i class="fas fa-cloud-arrow-down me-1"></i>${owned ? 'Borrow again' : 'Borrow &amp; Download'}</button>`;
            avail = '<span class="text-success small fw-semibold">Available now</span>';
        } else {
            action = `<button class="btn btn-sm btn-outline-warning" onclick="libbyPlaceHoldFmt('${fmt.titleId}','${card}',this)"><i class="fas fa-clock me-1"></i>Place hold</button>`;
            avail = `<span class="text-warning small">${esc(libbyWaitText(fmt.estimatedWaitDays))}</span>`;
        }
        rows.push(`<div class="d-flex align-items-center justify-content-between border rounded px-2 py-1 mb-1">
            <span>${label} · ${avail}</span>${action}</div>`);
    }
    return rows.length ? rows.join('') : '<div class="small text-muted">Not found on Libby.</div>';
}
// Place a hold on a specific format (thin wrapper over the existing hold flow).
async function libbyPlaceHoldFmt(titleId, cardId, btn) {
    if (!cardId) { showToast('No library card available for a hold.', 'warning'); return; }
    const orig = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>…'; }
    try {
        await GreatReads.apiCall('/libby/holds/place', { method: 'POST', data: { title_id: titleId, card_id: cardId }, silent: true });
        showToast('Hold placed.', 'success');
        if (btn) btn.innerHTML = '<i class="fas fa-check me-1"></i>On hold';
    } catch (e) { showToast(libbyErrMsg(e, 'hold'), 'danger'); if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
}

function libbyRenderAction() {
    const c = libbyActive; if (!c) return;
    const box = document.getElementById('libbyAction'); if (!box) return;
    // Unified result → per-format actions (#197).
    if (c.formats) { box.innerHTML = libbyFormatsHtml(c); return; }
    const libs = c.libraries || [];
    if (!libs.length) { box.innerHTML = ''; return; }
    if (c.onHold) { box.innerHTML = '<span class="badge bg-info text-dark"><i class="fas fa-clock me-1"></i>Already on hold</span>'; return; }
    const sel = document.getElementById('libbyCardSel');
    const opt = sel && sel.selectedOptions[0];
    const available = opt ? opt.dataset.avail === '1' : libs.some(l => l.isAvailable);
    // #191: audiobooks download via the bona-fide chip + Listen harvest; ebooks keep
    // the .acsm path. Route the borrow button to the right handler + label.
    const isAudio = (c.mediaType || (c._row && c._row.type)) === 'audiobook';
    const fn = isAudio ? 'libbyBorrowAudiobook()' : 'libbyBorrow()';
    const lbl = isAudio ? 'Borrow &amp; Download audiobook' : 'Borrow &amp; Download';
    const icon = isAudio ? 'fa-headphones' : 'fa-cloud-arrow-down';
    const borrowBtn = `<button id="libbyBorrowBtn" class="btn btn-sm btn-primary" onclick="${fn}"><i class="fas ${icon} me-2"></i>${lbl}</button>`;
    if (libbyIsOwned(c)) {
        // Owned → don't push a borrow; offer it only as a de-emphasized secondary.
        box.innerHTML = available
            ? `<button class="btn btn-sm btn-outline-secondary" onclick="${fn}"><i class="fas ${icon} me-2"></i>Borrow another copy</button>`
            : `<button id="libbyHoldBtn" class="btn btn-sm btn-outline-secondary" onclick="libbyPlaceHold()"><i class="fas fa-clock me-2"></i>Place hold</button>`;
        return;
    }
    box.innerHTML = available
        ? borrowBtn
        : `<button id="libbyHoldBtn" class="btn btn-sm btn-warning" onclick="libbyPlaceHold()"><i class="fas fa-clock me-2"></i>Place hold</button>
           <span class="small text-muted ms-2">Not available now at the selected library.</span>`;
}

// Open a Libby search result in the shared cover-tap popup with rich metadata,
// Borrow/Hold actions, and the FULL series (incl. foreign titles) from OverDrive.
// (book.series is blanked so the DB-only series strip doesn't duplicate ours.)
async function openLibbyDetails(c) {
    libbyActive = c;
    // Resolve real GreatReads ownership BEFORE rendering so the "In library" state is
    // correct on first paint (no flicker / false positive from a pending match).
    if (c.grOwned == null) { try { await annotateLibbyOwnership([c]); } catch (e) { c.grOwned = false; } }
    if (libbyActive !== c) return;   // superseded while awaiting
    const book = {
        id: null, title: c.title, author: c.author, series: '', universe: '',
        series_number: c.series_number, cover_url: c.cover_url || null,
        date_published: (c._row && c._row.publishDate) || '', genre: '',
        cover: false, inventory: [], readings: [],
    };
    // NOTE: the shared popup ignores opts.extraInfoHtml (retired in #120) — only
    // opts.actionsHtml renders. So the whole Libby panel goes through actionsHtml.
    const owned = libbyIsOwned(c)
        ? '<div class="alert alert-success py-1 px-2 small mb-2 w-100"><i class="fas fa-check-circle me-1"></i>Already in your GreatReads library.</div>' : '';
    // Add-to-Wishlist for titles you don't already have (#170) — handy when a title is
    // only holdable, or you just want to track it.
    const wishBtn = libbyIsOwned(c) ? ''
        : `<button class="btn btn-sm btn-outline-primary w-100 mb-2" id="libbyWishBtn" onclick="grLibbyAddWishlist(libbyActive)"><i class="fas fa-bookmark me-2"></i>Add to Wishlist</button>`;
    // Unified results carry per-format cards, so skip the single-card chooser (#197).
    const chooser = c.formats ? '' : libbyLibraryChooser(c);
    const actionsHtml = `${owned}${wishBtn}<div id="libbyRich" class="mb-2 w-100"></div>${chooser}<div id="libbyAction" class="mt-1 mb-2 w-100"></div><div id="libbySeries" class="w-100"></div>`;

    GreatReads.openBookActions(book, {
        title: c.title, actionsHtml,
        editBook: false, sessions: false, highlights: false,
        onShow: () => { libbyRenderAction(); libbyLoadRich(c); libbyLoadSeries(c); },
    });
}

// Rich metadata (synopsis, genre chips, ratings, publisher/language) from the engine.
async function libbyLoadRich(c) {
    const box = document.getElementById('libbyRich'); if (!box) return;
    box.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Loading details…</div>';
    let d;
    try {
        d = await GreatReads.apiCall('/libby/book-details', { method: 'POST', data: {
            title_id: c.title_id, book: { id: c.title_id, title: c.title, author: c.author, cover: c.cover_url, libraries: c.libraries },
        } });
    } catch (e) { box.innerHTML = ''; return; }
    if (libbyActive !== c) return;   // popup navigated away
    const b = (d && d.book) || d || {};
    const ratings = [];
    if (b.myRating != null) ratings.push(`<span class="badge bg-primary" title="Your rating in GreatReads">★ You ${b.myRating}</span>`);
    if (b.communityRating != null) ratings.push(`<span class="badge bg-secondary" title="Community rating">★ ${b.communityRating}</span>`);
    const meta = [];
    if (b.series && b.seriesIndex != null && b.seriesIndex !== '') meta.push(`${esc(b.series)} #${esc(String(b.seriesIndex))}`);
    if (b.publisher) meta.push(esc(b.publisher));
    if ((b.languages || []).length) meta.push(esc(b.languages[0]));
    if (b.pageCount) meta.push(`${b.pageCount} pp`);
    if (b.rating) meta.push(esc(b.rating));
    const chips = (b.subjects || []).slice(0, 10).map(s => `<span class="badge bg-light text-dark border me-1 mb-1">${esc(s)}</span>`).join('');
    const desc = (b.description || '').trim();
    const descHtml = desc
        ? `<div class="small mt-2" style="max-height:9em;overflow:auto;white-space:pre-line;text-align:justify;hyphens:auto;">${esc(desc)}</div>` : '';
    box.innerHTML = (ratings.length ? `<div class="mb-1">${ratings.join(' ')}</div>` : '')
        + (meta.length ? `<div class="small text-muted">${meta.join(' · ')}</div>` : '')
        + (chips ? `<div class="mt-2">${chips}</div>` : '')
        + descHtml;
}

// Full series (all titles, incl. ones not in our DB) with per-library availability.
let libbySeriesRows = [];
async function libbyLoadSeries(c) {
    const box = document.getElementById('libbySeries'); if (!box) return;
    const sid = (c._row && c._row.seriesId) || '';
    const sname = c.series || '';
    if (!sid && !sname) { box.innerHTML = ''; return; }
    box.innerHTML = '<hr class="my-2"><div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Loading series…</div>';
    const qs = sid ? ('series_id=' + encodeURIComponent(sid)) : ('series_name=' + encodeURIComponent(sname));
    let d;
    try { d = await GreatReads.apiCall('/libby/series-books?' + qs); }
    catch (e) { box.innerHTML = ''; return; }
    if (libbyActive !== c) return;
    let rows = (d && d.results) || [];
    if (rows.length <= 1) { box.innerHTML = ''; return; }
    rows.sort((a, b) => (parseFloat(a.seriesIndex) || 999) - (parseFloat(b.seriesIndex) || 999));
    libbySeriesRows = rows;
    // Tag each series entry with real GreatReads ownership so owned ones show
    // "In library" instead of an availability/wait badge (#164).
    rows.forEach(r => { r.kind = 'libby'; });
    try { await annotateLibbyOwnership(rows); } catch (e) { /* best-effort */ }
    if (libbyActive !== c) return;
    const mini = rows.map((r, i) => libbySeriesMini(r, i)).join('');
    box.innerHTML = `<hr class="my-2"><div class="text-muted small mb-2"><i class="fas fa-layer-group me-1"></i>In this series (${rows.length}) — full series on Libby</div><div class="row g-2">${mini}</div>`;
}

function libbySeriesMini(r, i) {
    const cover = r.cover
        ? `<img src="${r.cover}" alt="" loading="lazy" style="width:100%;height:100%;object-fit:cover;" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';"><div style="display:none;width:100%;height:100%;align-items:center;justify-content:center;"><i class="fas fa-book text-muted"></i></div>`
        : '<div style="display:flex;width:100%;height:100%;align-items:center;justify-content:center;"><i class="fas fa-book text-muted"></i></div>';
    const avail = libbyIsOwned(r) ? '<span class="badge bg-primary" style="font-size:.6rem;">In library</span>'
        : r.isAvailable ? '<span class="badge bg-success" style="font-size:.6rem;">Available</span>'
        : r.onHold ? '<span class="badge bg-info text-dark" style="font-size:.6rem;">On hold</span>'
        : `<span class="badge bg-warning text-dark" style="font-size:.6rem;">${esc(libbyWaitText(r.estimatedWaitDays))}</span>`;
    const idx = (r.seriesIndex !== '' && r.seriesIndex != null) ? `#${esc(String(r.seriesIndex))} ` : '';
    return `<div class="col-4 col-md-3"><div class="gba-clickable" style="cursor:pointer;" onclick="libbyOpenSeriesItem(${i})" title="${esc(dispTitle(r.title))}">
        <div style="aspect-ratio:2/3;background:#e9ecef;border-radius:4px;overflow:hidden;">${cover}</div>
        <div class="small text-truncate mt-1">${idx}${esc(dispTitle(r.title))}</div>
        <div>${avail}</div></div></div>`;
}

function libbyOpenSeriesItem(i) {
    const r = libbySeriesRows[i]; if (!r) return;
    openLibbyDetails(libbyRowToCard(r));
}

function _libbySelectedCard() {
    const sel = document.getElementById('libbyCardSel');
    if (sel && sel.value) return sel.value;
    const first = (libbyActive && libbyActive.libraries || [])[0];
    return first ? String(first.cardId) : '';
}

// Borrow → fulfill → .acsm → watcher → Calibre → GreatReads. ASYNC (#186): the engine
// borrow can take minutes via the OverDrive-website path — long enough to trip a
// reverse-proxy gateway timeout — so we kick it off in the background (returns a
// request_id immediately) and poll status. No request is held open for minutes.
function _libbyResetBorrowBtn() {
    const btn = document.getElementById('libbyBorrowBtn');
    const hold = document.getElementById('libbyHoldBtn');
    const isAudio = libbyActive && (libbyActive.mediaType || (libbyActive._row && libbyActive._row.type)) === 'audiobook';
    if (btn) {
        btn.disabled = false;
        btn.innerHTML = isAudio
            ? '<i class="fas fa-headphones me-2"></i>Borrow &amp; Download audiobook'
            : '<i class="fas fa-cloud-arrow-down me-2"></i>Borrow & Download';
    }
    if (hold) hold.disabled = false;
}
async function libbyBorrow(titleId, cardId, btn) {
    const c = libbyActive; if (!c) return;
    titleId = titleId || c.title_id;
    cardId = cardId || _libbySelectedCard();
    btn = btn || document.getElementById('libbyBorrowBtn');
    if (!cardId) { showToast('No library card available to borrow on.', 'warning'); return; }
    const orig = btn ? btn.innerHTML : '';
    const reset = () => { if (btn) { btn.disabled = false; btn.innerHTML = orig; } };
    const hold = document.getElementById('libbyHoldBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Borrowing…'; }
    if (hold) hold.disabled = true;

    let reqId;
    try {
        const r = await GreatReads.apiCall('/libby/download-async', { method: 'POST', data: { title_id: titleId, card_id: cardId, title: c.title }, silent: true });
        reqId = r && r.request_id;
    } catch (e) {
        showToast(libbyErrMsg(e, 'borrow'), 'danger'); reset(); return;
    }
    if (!reqId) { showToast('Couldn’t start the borrow — try again.', 'danger'); reset(); return; }
    showToast(`Borrowing “${c.title}” (ebook) — this runs in the background (up to a few minutes).`, 'info');

    const started = Date.now();
    const poll = async () => {
        let s = null;
        try { s = await GreatReads.apiCall('/libby/download-status?request_id=' + encodeURIComponent(reqId), { silent: true }); } catch (e) {}
        if (s && s.done) {
            if (s.ok) {
                showToast(`“${c.title}” ebook downloaded — importing into GreatReads shortly.`, 'success');
                if (btn) btn.innerHTML = '<i class="fas fa-check me-2"></i>Downloaded';
                c.inLibrary = true;
                if (typeof refreshImportBadge === 'function') refreshImportBadge();   // #137 — Store tray badge
            } else {
                showToast(s.detail || 'Borrow failed — check this card’s saved credentials in the Libby panel.', 'danger');
                reset();
            }
            return;
        }
        if (Date.now() - started > 5 * 60 * 1000) {   // safety cap ~5 min
            showToast('Still working in the background — check your Loans / Newly-Imported shortly.', 'info');
            reset();
            return;
        }
        if (btn && s && s.status) btn.innerHTML = `<span class="spinner-border spinner-border-sm me-2"></span>${s.status}…`;
        setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
}

// Audiobook borrow → download (#191). Unlike the ebook path this drives a headless
// Libby session to fulfil via the bona-fide (prbn:v) chip, harvest the OverDrive
// Listen player's MP3 parts into /audiobooks, and auto-return the loan. It's ONE
// background job in the engine (single global status), pollable while it runs.
async function libbyBorrowAudiobook(titleId, cardId, btn) {
    const c = libbyActive; if (!c) return;
    titleId = titleId || c.title_id;
    cardId = cardId || _libbySelectedCard();
    btn = btn || document.getElementById('libbyBorrowBtn');
    if (!cardId) { showToast('No library card available to borrow on.', 'warning'); return; }
    const orig = btn ? btn.innerHTML : '';
    const reset = () => { if (btn) { btn.disabled = false; btn.innerHTML = orig; } };
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Starting…'; }
    try {
        await GreatReads.apiCall('/libby/audiobook/download', { method: 'POST', silent: true,
            data: { title_id: titleId, card_id: cardId, title: c.title, borrow: true, return_after: true } });
    } catch (e) {
        showToast(libbyErrMsg(e, 'borrow'), 'danger'); reset(); return;
    }
    showToast(`Downloading “${c.title}” audiobook — this runs in the background (several minutes for a full book).`, 'info');
    const started = Date.now();
    const poll = async () => {
        let s = null;
        try { s = await GreatReads.apiCall('/libby/audiobook/download/status', { silent: true }); } catch (e) {}
        if (s && s.active === false && s.phase) {
            if (s.phase === 'done') {
                showToast(`“${s.title || c.title}” audiobook downloaded (${s.parts_total} parts) — Audiobookshelf will pick it up.`, 'success');
                if (btn) btn.innerHTML = '<i class="fas fa-check me-2"></i>Downloaded';
                c.inLibrary = true;
            } else if (s.phase === 'error') {
                showToast(s.message || 'Audiobook download failed.', 'danger');
                reset();
            } else {
                reset();
            }
            return;
        }
        if (Date.now() - started > 30 * 60 * 1000) {
            showToast('Still downloading in the background — check Audiobookshelf shortly.', 'info');
            reset(); return;
        }
        if (btn && s && s.active) {
            const prog = s.parts_total ? ` ${s.parts_done}/${s.parts_total}` : '';
            const ph = ({ borrow: 'Borrowing', chip: 'Preparing', open: 'Opening', download: 'Downloading', return: 'Returning' })[s.phase] || 'Working';
            btn.innerHTML = `<span class="spinner-border spinner-border-sm me-2"></span>${ph}${prog}…`;
        }
        setTimeout(poll, 4000);
    };
    setTimeout(poll, 3000);
}

// Human-readable Libby error from a failed borrow/hold (#4): OverDrive/engine statuses
// aren't always JSON, so map by status when there's no useful detail.
function libbyErrMsg(e, action) {
    const st = e && e.response && e.response.status;
    const data = e && e.response && e.response.data;
    const detail = data && (data.detail || data.error || data.message);
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (st === 409) return `Already borrowed on this card (or a loan/hold conflict) — check your Libby loans.`;
    if (st === 403) return `The library rejected this ${action}. The card may need website credentials in the Libby panel, or the title isn’t lendable to it.`;
    if (st === 404) return `That title isn’t available at the selected library right now.`;
    // A 5xx with NO detail is almost always a gateway timeout on the slow OverDrive
    // fulfill path (up to a few minutes) — the borrow may well have gone through, or
    // failed on the card's saved credentials. Don't call it an "engine outage" (#185).
    if (st >= 502 && st <= 504) return `This ${action} is taking longer than expected or the connection timed out — check your Loans (it may have gone through). If it keeps failing, re-check this card’s saved credentials in the Libby panel.`;
    return `Couldn’t complete the ${action}. Please try again.`;
}

async function libbyPlaceHold() {
    const c = libbyActive; if (!c) return;
    const cardId = _libbySelectedCard();
    if (!cardId) { showToast('No library card available.', 'warning'); return; }
    const btn = document.getElementById('libbyHoldBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Placing hold…'; }
    try {
        await GreatReads.apiCall('/libby/holds/place', { method: 'POST', data: { title_id: c.title_id, card_id: cardId }, silent: true });
        showToast('Hold placed.', 'success');
        if (btn) btn.innerHTML = '<i class="fas fa-check me-2"></i>On hold';
        grLibbyAddWishlist(c, true);   // #170: anything on hold is also on the Wishlist
    } catch (e) {
        showToast(libbyErrMsg(e, 'hold'), 'danger');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-clock me-2"></i>Place hold'; }
    }
}

// Add-to-Wishlist for a Libby card (#170): find-or-create an unowned DB record.
async function grLibbyAddWishlist(c, silent) {
    if (!c || !c.title) return;
    const btn = document.getElementById('libbyWishBtn');
    if (btn && !silent) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Adding…'; }
    let r;
    try {
        r = await GreatReads.apiCall('/libby/wishlist-add', { method: 'POST', silent: true, data: {
            title: c.title, author: c.author || '', series: c.series || '',
            series_number: c.series_number, cover_url: c.cover_url || (c._row && c._row.cover) || '',
        } });
    } catch (e) { if (!silent) { showToast('Couldn’t add to Wishlist.', 'danger'); if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-bookmark me-2"></i>Add to Wishlist'; } } return; }
    if (!silent) showToast(r.created ? `Added “${c.title}” to your Wishlist.` : `“${c.title}” is already in your library.`, r.created ? 'success' : 'info');
    if (btn) { btn.disabled = true; btn.innerHTML = `<i class="fas fa-check me-2"></i>${r.created ? 'On your Wishlist' : 'Already in library'}`; }
    return r;
}
