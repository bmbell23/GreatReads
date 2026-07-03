// Shared "Edit Book" modal (#110) — mounted on every page via base.html so the
// book-metadata editor (cover, custom typeahead #107, all fields) works in place
// everywhere, not just the Books page. Self-contained IIFE: uses GreatReads.*,
// keeps private state, and exposes the inline onclick handlers on window.
//
// Page-specific behavior is delegated to window.bkeHooks (all optional):
//   afterSave(id, data, book) — refresh the calling page after a save
//   afterDelete(id)           — refresh after a delete
//   nextId(afterId) -> id|null — "Save & Next" target (Books grid only; when
//                                absent the Save & Next button is hidden)
// Without hooks (e.g. a details page) the editor falls back to a page reload.
(function () {
    const esc = s => (s || '').replace(/[&<>"]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
    const toast = (m, t) => window.GreatReads && GreatReads.showToast && GreatReads.showToast(m, t);
    const api = (path, opts) => GreatReads.apiCall(path, opts);
    const hooks = () => window.bkeHooks || {};

    let bkeBook = null, bkeListsLoaded = false;
    // Create mode (#117): when adding a new book there's no id yet, so cover
    // endpoints (which need an id) can't run at pick-time — buffer the chosen
    // file/URL and upload it right after the create POST returns the new id.
    let bkeIsNew = false, bkePendingCoverFile = null, bkePendingCoverUrl = null;
    let bkePendingRating = null;   // #161: public_rating accepted while creating (no form field)
    const bkeSug = { first: [], last: [], series: [], universe: [], genre: [], title: [] };
    let bkeGenres = [];   // #156: the book's genres, edited as pills

    async function bkeLoadLists() {
        if (bkeListsLoaded) return;
        bkeListsLoaded = true;
        const load = async (path, key) => {
            try { const r = await api(path); bkeSug[key] = Array.isArray(r) ? r : (r.items || []); }
            catch (e) {}
        };
        const loadGenreVocab = async () => {
            // Genres now live in the Tag store (#149/#158); merge Tag names with the
            // legacy single-genre strings so the pill autocomplete offers everything the
            // library already uses (deduped, case-insensitively).
            let names = [];
            try {
                const [tags, genres] = await Promise.all([api('/books/search/tags'), api('/books/search/genres')]);
                names = [].concat(Array.isArray(tags) ? tags : [], Array.isArray(genres) ? genres : []);
            } catch (e) {}
            const seen = new Set(); const out = [];
            for (const n of names) { const low = String(n).toLowerCase(); if (n && !seen.has(low)) { seen.add(low); out.push(n); } }
            bkeSug.genre = out.sort((a, b) => a.localeCompare(b));
        };
        await Promise.all([
            load('/books/search/author-first-names', 'first'),
            load('/books/search/author-last-names', 'last'),
            load('/books/search/series', 'series'),
            loadGenreVocab(),
            load('/books/search/universes', 'universe'),
            load('/books/search/titles', 'title'),
        ]);
    }

    // Custom autocomplete (#107): WebView-safe dropdown (native <datalist> fails
    // there). Fixed-positioned on <body> so a scrollable modal can't clip it.
    function attachAutocomplete(inputId, key, onChoose) {
        const input = document.getElementById(inputId);
        if (!input || input.dataset.acWired) return;
        input.dataset.acWired = '1';
        input.setAttribute('autocomplete', 'off');
        input.removeAttribute('list');
        const menu = document.createElement('div');
        menu.className = 'ac-menu d-none';
        document.body.appendChild(menu);
        let items = [], active = -1;
        const place = () => { const r = input.getBoundingClientRect();
            menu.style.left = r.left + 'px'; menu.style.top = r.bottom + 'px'; menu.style.width = r.width + 'px'; };
        const hide = () => { menu.classList.add('d-none'); active = -1; };
        const hl = () => [...menu.children].forEach((c, i) => c.classList.toggle('ac-active', i === active));
        const render = () => {
            const q = input.value.trim().toLowerCase();
            const src = bkeSug[key] || [];
            let matches = src;
            if (q) { const s = [], h = [];
                for (const v of src) { const lv = String(v).toLowerCase();
                    if (lv.startsWith(q)) s.push(v); else if (lv.includes(q)) h.push(v); }
                matches = s.concat(h);
            }
            items = matches.slice(0, 8);
            if (!items.length) { hide(); return; }
            active = -1;
            menu.innerHTML = items.map((v, i) => `<div class="ac-item" data-i="${i}">${esc(String(v))}</div>`).join('');
            place(); menu.classList.remove('d-none');
        };
        // Pills mode (#156): onChoose consumes the value and the input clears for the
        // next entry, instead of the input holding a single value.
        const choose = v => { if (onChoose) { onChoose(v); input.value = ''; hide(); } else { input.value = v; hide(); } };
        input.addEventListener('focus', render);
        input.addEventListener('input', render);
        input.addEventListener('blur', () => setTimeout(hide, 150));
        input.addEventListener('keydown', e => {
            if (menu.classList.contains('d-none')) return;
            if (e.key === 'ArrowDown') { active = Math.min(active + 1, items.length - 1); hl(); e.preventDefault(); }
            else if (e.key === 'ArrowUp') { active = Math.max(active - 1, 0); hl(); e.preventDefault(); }
            else if (e.key === 'Enter') { if (active >= 0) { choose(items[active]); e.preventDefault(); } }
            else if (e.key === 'Escape') hide();
        });
        menu.addEventListener('pointerdown', e => {
            const it = e.target.closest('.ac-item'); if (!it) return;
            e.preventDefault(); choose(items[+it.dataset.i]);
        });
        window.addEventListener('resize', () => { if (!menu.classList.contains('d-none')) place(); });
        document.addEventListener('scroll', () => { if (!menu.classList.contains('d-none')) hide(); }, true);
    }

    function wireAutocomplete() {
        [['bkeAuthorFirst', 'first'], ['blkAuthorFirst', 'first'],
         ['bkeAuthorLast', 'last'], ['blkAuthorLast', 'last'],
         ['bkeSeries', 'series'], ['blkSeries', 'series'],
         ['bkeUniverse', 'universe'], ['blkUniverse', 'universe'],
         ['bkeTitle', 'title'],
        ].forEach(([id, key]) => attachAutocomplete(id, key));
        // Genres pill input (#156): selecting a suggestion adds a chip.
        attachAutocomplete('bkeGenreInput', 'genre', bkeAddGenre);
    }

    // ── Genre pills (#156) ───────────────────────────────────────────────────────
    function bkeRenderGenres() {
        const box = document.getElementById('bkeGenres');
        const input = document.getElementById('bkeGenreInput');
        if (!box || !input) return;
        box.querySelectorAll('.bke-genre-chip').forEach(c => c.remove());
        bkeGenres.forEach(name => {
            const chip = document.createElement('span');
            chip.className = 'bke-genre-chip';
            chip.innerHTML = `<span></span><button type="button" title="Remove" aria-label="Remove">&times;</button>`;
            chip.querySelector('span').textContent = name;
            chip.querySelector('button').addEventListener('click', () => bkeRemoveGenre(name));
            box.insertBefore(chip, input);
        });
    }
    function bkeAddGenre(name) {
        name = String(name || '').trim();
        if (!name) return;
        // Snap to an existing genre's canonical casing; else keep as typed (deduped).
        const canon = (bkeSug.genre || []).find(g => g.toLowerCase() === name.toLowerCase());
        const value = canon || name;
        if (!bkeGenres.some(g => g.toLowerCase() === value.toLowerCase())) {
            bkeGenres.push(value);
            if (!bkeSug.genre.some(g => g.toLowerCase() === value.toLowerCase())) bkeSug.genre.push(value);
            bkeRenderGenres();
        }
    }
    function bkeRemoveGenre(name) {
        bkeGenres = bkeGenres.filter(g => g.toLowerCase() !== String(name).toLowerCase());
        bkeRenderGenres();
    }
    function bkeGenreKey(e) {
        const input = e.target;
        if (e.key === 'Enter' || e.key === ',') {
            if (input.value.trim()) { bkeAddGenre(input.value); input.value = ''; e.preventDefault(); }
        } else if (e.key === 'Backspace' && !input.value && bkeGenres.length) {
            bkeRemoveGenre(bkeGenres[bkeGenres.length - 1]); e.preventDefault();
        }
    }
    function bkeSetGenres(list) { bkeGenres = Array.isArray(list) ? list.filter(Boolean).slice() : []; bkeRenderGenres(); }

    // Reset the modal chrome to whichever mode we're opening in: create mode
    // ("Add book", no Delete/Save & Next) vs edit mode ("Edit book", Delete shown).
    function bkeSetMode(isNew) {
        bkeIsNew = isNew;
        bkePendingCoverFile = null; bkePendingCoverUrl = null; bkePendingRating = null;
        const title = document.getElementById('bkeModalTitle');
        if (title) title.textContent = isNew ? 'Add book' : 'Edit book';
        const del = document.getElementById('bkeDeleteBtn');
        if (del) del.style.display = isNew ? 'none' : '';
        // "Request metadata" (#119) — in edit mode it applies to the saved book; in
        // create mode (#161) it looks up by title+author and fills the form.
        const enrich = document.getElementById('bkeEnrichBtn');
        if (enrich) enrich.style.display = '';
        const nb = document.getElementById('bkeSaveNextBtn');
        if (nb && isNew) nb.style.display = 'none';
        // Primary button reads "Create" when adding, "Save changes" when editing;
        // "Create & Another" (repeat-entry, Calibre-style) shows only in create mode.
        const save = document.getElementById('bkeSaveBtn');
        if (save) save.textContent = isNew ? 'Create' : 'Save changes';
        const another = document.getElementById('bkeCreateAnotherBtn');
        if (another) another.style.display = isNew ? '' : 'none';
        // Formats-owned picker: shown in BOTH add (#117) and edit — editing which
        // formats you own is really an inventory edit. Reset here; edit mode loads the
        // book's current ownership in bkeOpen, create mode leaves them unchecked.
        const fmt = document.getElementById('bkeFormatsRow');
        if (fmt) fmt.style.display = '';
        ['bkeOwnedEbook', 'bkeOwnedAudio', 'bkeOwnedPhysical']
            .forEach(id => { const el = document.getElementById(id); if (el) el.checked = false; });
    }

    // Current state of the Formats-owned toggles → inventory payload.
    const _bkeOwned = () => {
        const chk = id => !!document.getElementById(id)?.checked;
        return { owned_ebook: chk('bkeOwnedEbook'), owned_audio: chk('bkeOwnedAudio'),
                 owned_physical: chk('bkeOwnedPhysical') };
    };

    // Open the shared modal blank to create a brand-new book (#117), optionally
    // prefilled from a detected release ("Save locally" on the Books page, #68).
    // prefill keys map to field ids: title, author_first, author_last, series,
    // series_number, genre, date_published, page_count, word_count, cover_url.
    async function bkeOpenNew(prefill) {
        bkeLoadLists();
        bootstrap.Modal.getInstance(document.getElementById('bookDetailsModal'))?.hide();
        bootstrap.Modal.getInstance(document.getElementById('openBookModal'))?.hide();
        bkeBook = null;
        bkeSetMode(true);
        ['bkeId', 'bkeTitle', 'bkeAuthorFirst', 'bkeAuthorLast', 'bkeSeries', 'bkeSeriesNum',
         'bkeUniverse', 'bkeDate', 'bkePages', 'bkeWords', 'bkeIsbn', 'bkeCoverUrl', 'bkeDescription']
            .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
        bkeSetGenres([]);
        _bkeResetContributors();   // #192
        if (prefill) {
            const set = (id, v) => { const el = document.getElementById(id); if (el && v != null && v !== '') el.value = v; };
            set('bkeTitle', prefill.title);
            set('bkeAuthorFirst', prefill.author_first);
            set('bkeAuthorLast', prefill.author_last);
            set('bkeSeries', prefill.series);
            set('bkeSeriesNum', prefill.series_number);
            if (prefill.genre) bkeAddGenre(prefill.genre);
            set('bkeDate', prefill.date_published);
            set('bkePages', prefill.page_count);
            set('bkeWords', prefill.word_count);
            // No id yet — buffer the cover URL so it downloads right after the create POST.
            if (prefill.cover_url) { bkePendingCoverUrl = prefill.cover_url; bkePendingCoverFile = null; }
        }
        bkeRenderCover();
        bootstrap.Modal.getOrCreateInstance(document.getElementById('bkEditModal')).show();
        setTimeout(() => document.getElementById('bkeTitle')?.focus(), 300);
    }

    async function bkeOpen(bookId) {
        bkeLoadLists();
        // close whichever popup launched us (Books details modal or the shared
        // book-actions popup) so the editor isn't stacked on top of it. Route the
        // shared popup through grHidePopupForEdit so Cancel/Save returns to it (#157).
        bootstrap.Modal.getInstance(document.getElementById('bookDetailsModal'))?.hide();
        if (typeof grHidePopupForEdit === 'function') grHidePopupForEdit();
        else bootstrap.Modal.getInstance(document.getElementById('openBookModal'))?.hide();
        let b;
        try { b = await api('/books/' + bookId); }
        catch (e) { toast('Could not load book', 'danger'); return; }
        bkeBook = b;
        bkeSetMode(false);
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = (v != null ? v : ''); };
        set('bkeId', b.id); set('bkeTitle', b.title);
        set('bkeAuthorFirst', b.author_name_first); set('bkeAuthorLast', b.author_name_second);
        set('bkeSeries', b.series); set('bkeSeriesNum', b.series_number);
        set('bkeUniverse', b.universe); set('bkeDate', b.date_published);
        set('bkePages', b.page_count); set('bkeWords', b.word_count); set('bkeIsbn', b.isbn_id);
        set('bkeCoverUrl', '');
        set('bkeDescription', b.description);
        // Genres from the tag store (#156); fall back to the legacy single genre.
        bkeSetGenres((Array.isArray(b.tags) && b.tags.length) ? b.tags : (b.genre ? [b.genre] : []));
        await _bkeLoadContributors(b.id);   // #192 additional authors/narrators
        bkeRenderCover();
        // Load the book's current format ownership into the picker (edit mode).
        try {
            const inv = await api('/inventory/?book_id=' + b.id);
            const row = Array.isArray(inv) ? inv[0] : null;
            const setChk = (cid, val) => { const el = document.getElementById(cid); if (el) el.checked = !!val; };
            setChk('bkeOwnedEbook', row && row.owned_ebook);
            setChk('bkeOwnedAudio', row && row.owned_audio);
            setChk('bkeOwnedPhysical', row && row.owned_physical);
        } catch (e) { /* leave unchecked if inventory can't be read */ }
        // "Save & Next" only where the page provides a nav order (Books grid, #104)
        const nb = document.getElementById('bkeSaveNextBtn');
        if (nb) {
            const nextFn = hooks().nextId;
            if (!nextFn) { nb.style.display = 'none'; }
            else { nb.style.display = ''; nb.disabled = nextFn(b.id) == null; }
        }
        bootstrap.Modal.getOrCreateInstance(document.getElementById('bkEditModal')).show();
    }

    function bkeRenderCover() {
        const base = window.APP_BASE_PATH || '', b = bkeBook;
        // Standalone "No cover" (visible). When it trails an <img> it must start hidden —
        // the flex container would otherwise render it beside the cover (half-cover bug) —
        // and the img's onerror re-shows it only if the image fails to load.
        const fallback = `<div class="bke-cover-empty">No cover</div>`;
        const hiddenFallback = `<div class="bke-cover-empty" style="display:none;">No cover</div>`;
        const img = src => `<img src="${src}" alt="" style="width:100%;height:100%;object-fit:cover;" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">${hiddenFallback}`;
        let html = fallback;
        if (bkeIsNew) {
            // Preview the buffered pick before it's uploaded (no id yet, #117).
            if (bkePendingCoverFile) html = img(URL.createObjectURL(bkePendingCoverFile));
            else if (bkePendingCoverUrl) html = img(bkePendingCoverUrl);
        } else if (b && b.cover) {
            html = img(`${base}/static/covers/${b.id}.jpg?v=${Date.now()}`);
        }
        document.getElementById('bkeCover').innerHTML = html;
    }

    // PUT the fields; on success let the page refresh itself (afterSave hook) or
    // fall back to a reload. Returns the saved id, or null on failure.
    // Additional authors/narrators (#192) — dynamic first/last rows.
    function bkeContribRow(role, first, last) {
        const wrap = document.getElementById(role === 'author' ? 'bkeAddAuthors' : 'bkeAddNarrators');
        if (!wrap) return;
        const row = document.createElement('div');
        row.className = 'd-flex gap-2 mt-1 bke-contrib-row';
        row.innerHTML =
            '<input class="form-control form-control-sm bke-cf" placeholder="First name" autocomplete="off">' +
            '<input class="form-control form-control-sm bke-cl" placeholder="Last name" autocomplete="off">' +
            '<button type="button" class="btn btn-sm btn-outline-secondary" title="Remove" onclick="this.parentNode.remove()">&times;</button>';
        row.querySelector('.bke-cf').value = first || '';
        row.querySelector('.bke-cl').value = last || '';
        wrap.appendChild(row);
    }
    function bkeAddContribRow(role) { bkeContribRow(role, '', ''); }
    function _bkeContribRows(wrapId) {
        return [...document.getElementById(wrapId).querySelectorAll('.bke-contrib-row')]
            .map(r => ({ first: r.querySelector('.bke-cf').value.trim(), last: r.querySelector('.bke-cl').value.trim() }))
            .filter(x => x.first || x.last);
    }
    function _bkeGatherContributors() {
        const val = id => (document.getElementById(id).value || '').trim();
        const authors = [{ first: val('bkeAuthorFirst'), last: val('bkeAuthorLast') }, ..._bkeContribRows('bkeAddAuthors')].filter(x => x.first || x.last);
        const narrators = [{ first: val('bkeNarratorFirst'), last: val('bkeNarratorLast') }, ..._bkeContribRows('bkeAddNarrators')].filter(x => x.first || x.last);
        return { authors, narrators };
    }
    async function _bkeSaveContributors(id) {
        try { await api('/books/' + id + '/contributors', { method: 'POST', data: _bkeGatherContributors() }); }
        catch (e) { /* non-fatal — primary author/narrator already saved on the book */ }
    }
    function _bkeResetContributors() {
        ['bkeAddAuthors', 'bkeAddNarrators'].forEach(w => { const el = document.getElementById(w); if (el) el.innerHTML = ''; });
        ['bkeNarratorFirst', 'bkeNarratorLast'].forEach(i => { const el = document.getElementById(i); if (el) el.value = ''; });
    }
    async function _bkeLoadContributors(bookId) {
        _bkeResetContributors();
        try {
            const cc = await api('/books/' + bookId + '/contributors');
            (cc.authors || []).slice(1).forEach(c => bkeContribRow('author', c.first, c.last));
            const narr = cc.narrators || [];
            if (narr.length) {
                const n0 = narr[0];
                const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = (v != null ? v : ''); };
                set('bkeNarratorFirst', n0.first); set('bkeNarratorLast', n0.last);
                narr.slice(1).forEach(c => bkeContribRow('narrator', c.first, c.last));
            }
        } catch (e) { /* leave blank */ }
    }

    async function _bkeSaveCore() {
        const id = document.getElementById('bkeId').value;
        const v = id2 => { const x = document.getElementById(id2).value.trim(); return x === '' ? null : x; };
        const num = (id2, fn) => { const x = document.getElementById(id2).value; return x === '' ? null : fn(x); };
        const data = {
            title: document.getElementById('bkeTitle').value.trim(),
            author_name_first: v('bkeAuthorFirst'), author_name_second: v('bkeAuthorLast'),
            series: v('bkeSeries'), universe: v('bkeUniverse'),
            // Genres are pills now (#156): persist the set; keep the legacy single genre
            // in sync as the primary (first) so existing genre filters keep working.
            tags: bkeGenres.slice(), genre: bkeGenres[0] || null,
            description: v('bkeDescription'),
            series_number: num('bkeSeriesNum', parseFloat),
            date_published: document.getElementById('bkeDate').value || null,
            page_count: num('bkePages', parseInt), word_count: num('bkeWords', parseInt),
            isbn_id: num('bkeIsbn', parseInt),
        };
        // Public rating has no form field; carry an enrichment-accepted value through (#161).
        if (bkePendingRating != null) data.public_rating = bkePendingRating;
        if (bkeIsNew) return _bkeCreate(data);
        try {
            await api('/books/' + id, { method: 'PUT', data });
            // Persist the format-ownership toggles (upsert; all-false → book becomes unowned).
            const owned = _bkeOwned();
            let book = bkeBook;
            try {
                await api('/inventory/book/' + id, { method: 'PUT', data: owned });
                book = { ...(bkeBook || {}), ...owned,
                         is_owned: owned.owned_ebook || owned.owned_audio || owned.owned_physical };
            } catch (e) { toast('Saved, but ownership didn’t update', 'warning'); }
            await _bkeSaveContributors(id);   // #192 authors + narrators
            toast('Book updated', 'success');
            const after = hooks().afterSave;
            if (after) after(parseInt(id, 10), data, book);
            return parseInt(id, 10);
        } catch (e) { toast('Save failed', 'danger'); return null; }
    }

    // Create a new book (#117): POST the fields, then — because cover endpoints
    // need the id — upload the buffered cover pick against the returned id.
    async function _bkeCreate(data) {
        // Title is optional (title-less placeholders for unreleased series entries are a
        // supported state — a book can also have its title cleared on edit). Just guard
        // against a completely empty record.
        if (!data.title && !data.series && !data.author_name_first && !data.author_name_second) {
            toast('Add at least a title, series, or author', 'warning'); return null;
        }
        let book;
        try { book = await api('/books/', { method: 'POST', data }); }
        catch (e) { toast('Could not add book', 'danger'); return null; }
        const id = book.id;
        try {
            if (bkePendingCoverFile) {
                const fd = new FormData(); fd.append('file', bkePendingCoverFile);
                await api(`/books/${id}/cover`, { method: 'POST', data: fd });
                book.cover = true;
            } else if (bkePendingCoverUrl) {
                await api(`/books/${id}/cover/from-url`, { method: 'POST', data: { url: bkePendingCoverUrl } });
                book.cover = true;
            }
        } catch (e) { toast('Book added, but the cover failed', 'warning'); }
        // Formats owned (#117): upsert an inventory row so the book is marked
        // owned in the picked formats (empty selection = stays unowned).
        const chk = elId => !!document.getElementById(elId)?.checked;
        const owned = { owned_ebook: chk('bkeOwnedEbook'), owned_audio: chk('bkeOwnedAudio'), owned_physical: chk('bkeOwnedPhysical') };
        book.is_owned = owned.owned_ebook || owned.owned_audio || owned.owned_physical;
        if (book.is_owned) {
            try { await api(`/inventory/book/${id}`, { method: 'PUT', data: owned }); }
            catch (e) { toast('Book added, but ownership didn’t save', 'warning'); book.is_owned = false; }
        }
        await _bkeSaveContributors(id);   // #192 authors + narrators
        toast('Book added', 'success');
        const after = hooks().afterCreate || hooks().afterSave;
        if (after) after(id, data, book);
        return id;
    }

    async function bkeSave() {
        if (await _bkeSaveCore() != null)
            bootstrap.Modal.getInstance(document.getElementById('bkEditModal'))?.hide();
    }

    async function bkeSaveAndNext() {
        const curId = parseInt(document.getElementById('bkeId').value, 10);
        const nextFn = hooks().nextId;
        const nextId = nextFn ? nextFn(curId) : null;   // resolve before save (order stable)
        if (await _bkeSaveCore() == null) return;
        if (nextId != null) {
            bkeOpen(nextId);
        } else {
            bootstrap.Modal.getInstance(document.getElementById('bkEditModal'))?.hide();
            toast('Saved — that was the last book in this view.', 'info');
        }
    }

    // Create this book, then keep the modal open for the next one — carrying over
    // author/series/universe/genre and bumping the series number (Calibre-style repeat add).
    async function bkeCreateAnother() {
        const g = id => document.getElementById(id);
        // Snapshot carry-over fields before the create (create doesn't mutate inputs).
        const carry = {
            first: g('bkeAuthorFirst').value, last: g('bkeAuthorLast').value,
            series: g('bkeSeries').value, universe: g('bkeUniverse').value,
            genres: bkeGenres.slice(), snum: g('bkeSeriesNum').value,
        };
        if (await _bkeSaveCore() == null) return;   // validation/error → stay put
        // Reset to a fresh create, then repopulate the carry-overs + incremented series #.
        bkeBook = null;
        bkeSetMode(true);   // resets chrome, formats, cover buffers; keeps text inputs
        ['bkeId', 'bkeTitle', 'bkeDate', 'bkePages', 'bkeWords', 'bkeIsbn', 'bkeCoverUrl', 'bkeDescription']
            .forEach(id => { const el = g(id); if (el) el.value = ''; });
        g('bkeAuthorFirst').value = carry.first;
        g('bkeAuthorLast').value = carry.last;
        g('bkeSeries').value = carry.series;
        g('bkeUniverse').value = carry.universe;
        bkeSetGenres(carry.genres);   // carry genres over to the next entry (#156)
        const n = parseFloat(carry.snum);
        g('bkeSeriesNum').value = Number.isFinite(n) ? n + 1 : '';
        bkeRenderCover();
        setTimeout(() => g('bkeTitle')?.focus(), 100);
    }

    async function bkeDelete() {
        const id = document.getElementById('bkeId').value;
        const title = document.getElementById('bkeTitle').value || 'this book';
        if (!confirm(`Delete “${title}”?\n\nThis permanently removes the book and its reading/inventory records. This can't be undone.`)) return;
        try {
            await api('/books/' + id, { method: 'DELETE' });
            toast('Book deleted', 'success');
            bootstrap.Modal.getInstance(document.getElementById('bkEditModal'))?.hide();
            const after = hooks().afterDelete;
            if (after) after(parseInt(id, 10));
            else if (typeof window.location !== 'undefined') { /* details pages reload their own list */ }
        } catch (e) { toast('Delete failed', 'danger'); }
    }

    async function bkeUploadFile() {
        const f = document.getElementById('bkeCoverFile').files[0];
        if (!f) return;
        if (bkeIsNew) {   // no id yet — buffer for upload after create (#117)
            bkePendingCoverFile = f; bkePendingCoverUrl = null; bkeRenderCover();
            document.getElementById('bkeCoverFile').value = ''; return;
        }
        const id = document.getElementById('bkeId').value, fd = new FormData();
        fd.append('file', f);
        try { await api(`/books/${id}/cover`, { method: 'POST', data: fd });
            if (bkeBook) bkeBook.cover = true; bkeRenderCover(); toast('Cover updated', 'success'); }
        catch (e) { toast('Upload failed', 'danger'); }
        document.getElementById('bkeCoverFile').value = '';
    }

    async function bkeCoverFromUrl() {
        const url = document.getElementById('bkeCoverUrl').value.trim();
        if (!url) return;
        if (bkeIsNew) {   // no id yet — buffer for download after create (#117)
            bkePendingCoverUrl = url; bkePendingCoverFile = null; bkeRenderCover();
            document.getElementById('bkeCoverUrl').value = ''; return;
        }
        const id = document.getElementById('bkeId').value;
        try { await api(`/books/${id}/cover/from-url`, { method: 'POST', data: { url } });
            if (bkeBook) bkeBook.cover = true; bkeRenderCover();
            document.getElementById('bkeCoverUrl').value = ''; toast('Cover downloaded', 'success'); }
        catch (e) { toast('Could not fetch image', 'danger'); }
    }

    async function bkeRemoveCover() {
        if (bkeIsNew) {   // just discard the buffered pick (#117)
            bkePendingCoverFile = null; bkePendingCoverUrl = null; bkeRenderCover(); return;
        }
        const id = document.getElementById('bkeId').value;
        try { await api(`/books/${id}/cover`, { method: 'DELETE' });
            if (bkeBook) bkeBook.cover = false; bkeRenderCover(); toast('Cover removed', 'success'); }
        catch (e) { toast('Remove failed', 'danger'); }
    }

    // ---- Request metadata (#119): look up one book online, accept per field ----
    // Every row defaults to "Keep current" (reject). Applying writes only the
    // accepted fields via the existing PUT /books + cover-from-url endpoints, and
    // syncs the Edit Book inputs so a later "Save changes" stays consistent.
    const bkeMetaFieldToInput = {
        date_published: 'bkeDate', page_count: 'bkePages',
        series_number: 'bkeSeriesNum', description: 'bkeDescription',
    };

    async function bkeRequestMetadata() {
        const id = document.getElementById('bkeId').value;
        // Create mode (#161): no id yet → look up by the form's title + author and fill
        // the form on accept. Edit mode: look up the saved book and apply via PUT.
        let request;
        if (id) {
            request = api('/enrichment/' + id + '/suggest', { method: 'POST' });
        } else {
            const title = document.getElementById('bkeTitle').value.trim();
            if (!title) { toast('Enter a title first', 'info'); return; }
            const author = [document.getElementById('bkeAuthorFirst').value.trim(),
                            document.getElementById('bkeAuthorLast').value.trim()].filter(Boolean).join(' ');
            request = api('/enrichment/lookup', { method: 'POST', data: { title, author } });
        }
        const body = document.getElementById('bkeMetaBody');
        const sub = document.getElementById('bkeMetaSub');
        body.innerHTML = '<div class="text-center text-muted py-4">'
            + '<i class="fas fa-spinner fa-spin me-2"></i>Searching Apple Books + Google + OpenLibrary…</div>';
        sub.textContent = '';
        bootstrap.Modal.getOrCreateInstance(document.getElementById('bkeMetaModal')).show();
        let res;
        try { res = await request; }
        catch (e) { body.innerHTML = '<div class="text-danger py-3">Lookup failed.</div>'; return; }
        bkeRenderMeta(res);
    }

    function bkeRenderMeta(res) {
        const body = document.getElementById('bkeMetaBody');
        const sub = document.getElementById('bkeMetaSub');
        const q = res.query || {};
        sub.textContent = 'Looked up by ' + (q.mode === 'isbn' ? 'ISBN ' + q.isbn : 'title + author')
            + '. Nothing is accepted unless you choose it.';
        const fields = res.fields || [];
        if (!fields.length) {
            body.innerHTML = '<div class="text-muted py-3">No suggestions found for this book.</div>';
            return;
        }
        const base = window.APP_BASE_PATH || '';
        const curCoverImg = id => `${base}/static/covers/${id}.jpg?v=${Date.now()}`;
        body.innerHTML = fields.map(f => {
            const name = 'bkemeta-' + f.field;
            const srcBadge = c => {
                const cls = c.agree ? 'bke-meta-src bke-meta-agree' : 'bke-meta-src';
                return `<span class="${cls}">${c.agree ? 'confirmed · ' : ''}${esc(c.source)}</span>`;
            };
            // Cover field (#130): render Current + each candidate as big side-by-side
            // cards so the covers are actually comparable. Radio semantics are unchanged
            // (`-keep` = reject; `data-cover-url` on candidates) so bkeApplyMetadata works.
            if (f.is_cover) {
                const card = (rid, checked, imgSrc, caption, dataAttr) => `
                    <label class="bke-cover-opt${checked ? ' bke-cover-opt-sel' : ''}" for="${rid}">
                        <input class="form-check-input" type="radio" name="${name}" id="${rid}"${checked ? ' checked' : ''}${dataAttr}>
                        <div class="bke-cover-opt-img">${imgSrc
                            ? `<img src="${imgSrc}" alt="" onerror="this.parentNode.innerHTML='<span class=\\'bke-meta-cur\\'>no image</span>'">`
                            : `<span class="bke-meta-cur">none</span>`}</div>
                        <div class="bke-cover-opt-cap">${caption}</div>
                    </label>`;
                const cards = [card(`${name}-keep`, true,
                    f.current ? curCoverImg(res.book_id) : '', 'Keep current', '')];
                f.candidates.forEach((c, i) => {
                    cards.push(card(`${name}-${i}`, false, esc(c.url),
                        `Use this ${srcBadge(c)}`, ` data-cover-url="${esc(c.url)}"`));
                });
                return `<div class="bke-meta-field mb-3" data-field="${f.field}" data-cover="1">
                    <div class="fw-bold small mb-1">${esc(f.label)}</div>
                    <div class="bke-meta-covers">${cards.join('')}</div></div>`;
            }
            // Genres (#158): multi-select. Genres already on the book are pre-checked;
            // unchecking removes them, checking a suggestion adds it. Apply unions the
            // checked set into the book's Genres (never called "tags" in the UI).
            if (f.kind === 'genres') {
                const curLower = new Set((f.current || []).map(x => x.toLowerCase()));
                const seen = new Set();
                const box = (name, checked, badge) => {
                    seen.add(name.toLowerCase());
                    return `<label class="bke-genre-opt${checked ? ' bke-genre-opt-sel' : ''}">
                        <input class="form-check-input me-1" type="checkbox" value="${esc(name)}"${checked ? ' checked' : ''}>
                        <span>${esc(name)}</span>${badge}</label>`;
                };
                const boxes = [];
                // Current genres first (pre-checked), then suggestions not already shown.
                (f.current || []).forEach(n => boxes.push(box(n, true, '')));
                f.candidates.forEach(c => {
                    if (!seen.has(String(c.value).toLowerCase())) boxes.push(box(c.value, c.on_book, srcBadge(c)));
                });
                return `<div class="bke-meta-field mb-3" data-field="genres" data-kind="genres"
                        data-current="${esc((f.current || []).join('|'))}">
                    <div class="fw-bold small mb-1">${esc(f.label)}</div>
                    <div class="bke-genre-opts d-flex flex-wrap gap-2">${boxes.join('')}</div></div>`;
            }
            // Default (checked) "keep current" row = reject.
            const cur = (f.current === null || f.current === undefined || f.current === '') ? '—' : esc(String(f.current));
            const curLabel = `Keep current <span class="bke-meta-cur">(${cur})</span>`;
            const rows = [`<div class="form-check">
                <input class="form-check-input" type="radio" name="${name}" id="${name}-keep" checked>
                <label class="form-check-label" for="${name}-keep">${curLabel}</label></div>`];
            f.candidates.forEach((c, i) => {
                const rid = `${name}-${i}`;
                const label = `${esc(String(c.display))}${srcBadge(c)}`;
                rows.push(`<div class="form-check">
                    <input class="form-check-input" type="radio" name="${name}" id="${rid}" data-value="${esc(String(c.value))}">
                    <label class="form-check-label" for="${rid}">${label}</label></div>`);
            });
            return `<div class="bke-meta-field mb-3" data-field="${f.field}" data-cover="0">
                <div class="fw-bold small mb-1">${esc(f.label)}</div>${rows.join('')}</div>`;
        }).join('');
        // Move the selected-card highlight as the user picks a cover (no :has() reliance,
        // for older Android WebView). #130
        body.querySelectorAll('.bke-meta-covers').forEach(group => {
            group.addEventListener('change', () => {
                group.querySelectorAll('.bke-cover-opt').forEach(opt =>
                    opt.classList.toggle('bke-cover-opt-sel', opt.querySelector('input').checked));
            });
        });
        body.querySelectorAll('.bke-genre-opts').forEach(group => {
            group.addEventListener('change', () => {
                group.querySelectorAll('.bke-genre-opt').forEach(opt =>
                    opt.classList.toggle('bke-genre-opt-sel', opt.querySelector('input').checked));
            });
        });
    }

    async function bkeApplyMetadata() {
        const id = document.getElementById('bkeId').value;
        const data = {};          // book fields to PUT
        let coverUrl = null;
        const norm = a => [...new Set(a.map(s => s.toLowerCase()))].sort().join('');
        document.querySelectorAll('#bkeMetaBody .bke-meta-field').forEach(group => {
            // Genres (#158): multi-select checkboxes → union set. Apply only if the
            // chosen set differs from what was already on the book.
            if (group.dataset.kind === 'genres') {
                const names = [...group.querySelectorAll('input[type=checkbox]:checked')]
                    .map(x => x.value.trim()).filter(Boolean);
                const cur = (group.dataset.current || '').split('|').filter(Boolean);
                if (norm(names) !== norm(cur)) {
                    data.tags = names;                         // PUT replaces the Genres set
                    // Backfill the legacy single genre if the book has none yet (never clobber).
                    if (names.length && !(bkeGenres.length || (bkeBook && bkeBook.genre)))
                        data.genre = names[0];
                }
                return;
            }
            const sel = group.querySelector('input[type=radio]:checked');
            if (!sel || sel.id.endsWith('-keep')) return;   // "keep current" → reject
            if (group.dataset.cover === '1') coverUrl = sel.dataset.coverUrl || null;
            else {
                const field = group.dataset.field, raw = sel.dataset.value;
                if (field === 'page_count') data[field] = parseInt(raw, 10);
                else if (field === 'series_number' || field === 'public_rating') data[field] = parseFloat(raw);
                else data[field] = raw;   // date_published (ISO) / description / genre
            }
        });
        if (!Object.keys(data).length && !coverUrl) {
            toast('Nothing selected to accept', 'info'); return;
        }
        // Edit mode: write immediately (PUT + cover-from-url). Create mode (#161): there's
        // no id yet, so buffer the cover + rating and just fill the form; the create POST
        // persists everything together.
        if (id) {
            try {
                if (Object.keys(data).length) await api('/books/' + id, { method: 'PUT', data });
                if (coverUrl) {
                    await api(`/books/${id}/cover/from-url`, { method: 'POST', data: { url: coverUrl } });
                    if (bkeBook) bkeBook.cover = true;
                }
            } catch (e) { toast('Apply failed', 'danger'); return; }
        } else {
            if (coverUrl) { bkePendingCoverUrl = coverUrl; bkePendingCoverFile = null; }
            if (data.public_rating != null) bkePendingRating = data.public_rating;
        }
        // Sync the Edit Book inputs (+ bkeBook) so the visible form matches what we
        // just wrote/buffered — otherwise a later "Save changes" would revert these.
        Object.entries(data).forEach(([field, val]) => {
            const inputId = bkeMetaFieldToInput[field];
            const el = inputId && document.getElementById(inputId);
            if (el) el.value = (val == null ? '' : val);
            if (bkeBook) bkeBook[field] = val;
        });
        if (data.tags) bkeSetGenres(data.tags);   // #156: reflect accepted genres in the pills
        bkeRenderCover();
        toast('Metadata applied', 'success');
        if (id) { const after = hooks().afterSave; if (after) after(parseInt(id, 10), data, bkeBook); }
        bootstrap.Modal.getInstance(document.getElementById('bkeMetaModal'))?.hide();
    }

    // Expose the handlers referenced by inline onclick= in the modal markup.
    Object.assign(window, {
        bkeOpen, bkeOpenNew, bkeSave, bkeSaveAndNext, bkeCreateAnother, bkeDelete,
        bkeAddContribRow,
        bkeUploadFile, bkeCoverFromUrl, bkeRemoveCover, bkeLoadLists, wireBookEdit: wireAutocomplete,
        bkeRequestMetadata, bkeApplyMetadata,
        bkeGenreKey, bkeAddGenre, bkeRemoveGenre,
        // Shared so the bulk-edit modals reuse the same WebView-safe autocomplete + the
        // genre vocabulary (tags + genres), instead of a native <datalist> (#1/#3).
        grAttachAutocomplete: attachAutocomplete,
    });

    document.addEventListener('DOMContentLoaded', () => { wireAutocomplete(); bkeLoadLists(); });
})();
