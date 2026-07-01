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
    const bkeSug = { first: [], last: [], series: [], universe: [], genre: [], title: [] };

    async function bkeLoadLists() {
        if (bkeListsLoaded) return;
        bkeListsLoaded = true;
        const load = async (path, key) => {
            try { const r = await api(path); bkeSug[key] = Array.isArray(r) ? r : (r.items || []); }
            catch (e) {}
        };
        await Promise.all([
            load('/books/search/author-first-names', 'first'),
            load('/books/search/author-last-names', 'last'),
            load('/books/search/series', 'series'),
            load('/books/search/genres', 'genre'),
            load('/books/search/universes', 'universe'),
            load('/books/search/titles', 'title'),
        ]);
    }

    // Custom autocomplete (#107): WebView-safe dropdown (native <datalist> fails
    // there). Fixed-positioned on <body> so a scrollable modal can't clip it.
    function attachAutocomplete(inputId, key) {
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
        const choose = v => { input.value = v; hide(); };
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
         ['bkeGenre', 'genre'], ['blkGenre', 'genre'],
         ['bkeTitle', 'title'],
        ].forEach(([id, key]) => attachAutocomplete(id, key));
    }

    // Reset the modal chrome to whichever mode we're opening in: create mode
    // ("Add book", no Delete/Save & Next) vs edit mode ("Edit book", Delete shown).
    function bkeSetMode(isNew) {
        bkeIsNew = isNew;
        bkePendingCoverFile = null; bkePendingCoverUrl = null;
        const title = document.getElementById('bkeModalTitle');
        if (title) title.textContent = isNew ? 'Add book' : 'Edit book';
        const del = document.getElementById('bkeDeleteBtn');
        if (del) del.style.display = isNew ? 'none' : '';
        const nb = document.getElementById('bkeSaveNextBtn');
        if (nb && isNew) nb.style.display = 'none';
        // Formats-owned picker is add-only (#117); reset it each time we open new.
        const fmt = document.getElementById('bkeFormatsRow');
        if (fmt) fmt.style.display = isNew ? '' : 'none';
        if (isNew) ['bkeOwnedEbook', 'bkeOwnedAudio', 'bkeOwnedPhysical']
            .forEach(id => { const el = document.getElementById(id); if (el) el.checked = false; });
    }

    // Open the shared modal blank to create a brand-new book (#117).
    async function bkeOpenNew() {
        bkeLoadLists();
        bootstrap.Modal.getInstance(document.getElementById('bookDetailsModal'))?.hide();
        bootstrap.Modal.getInstance(document.getElementById('openBookModal'))?.hide();
        bkeBook = null;
        bkeSetMode(true);
        ['bkeId', 'bkeTitle', 'bkeAuthorFirst', 'bkeAuthorLast', 'bkeSeries', 'bkeSeriesNum',
         'bkeUniverse', 'bkeGenre', 'bkeDate', 'bkePages', 'bkeWords', 'bkeIsbn', 'bkeCoverUrl']
            .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
        bkeRenderCover();
        bootstrap.Modal.getOrCreateInstance(document.getElementById('bkEditModal')).show();
        setTimeout(() => document.getElementById('bkeTitle')?.focus(), 300);
    }

    async function bkeOpen(bookId) {
        bkeLoadLists();
        // close whichever popup launched us (Books details modal or the shared
        // book-actions popup) so the editor isn't stacked on top of it
        bootstrap.Modal.getInstance(document.getElementById('bookDetailsModal'))?.hide();
        bootstrap.Modal.getInstance(document.getElementById('openBookModal'))?.hide();
        let b;
        try { b = await api('/books/' + bookId); }
        catch (e) { toast('Could not load book', 'danger'); return; }
        bkeBook = b;
        bkeSetMode(false);
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = (v != null ? v : ''); };
        set('bkeId', b.id); set('bkeTitle', b.title);
        set('bkeAuthorFirst', b.author_name_first); set('bkeAuthorLast', b.author_name_second);
        set('bkeSeries', b.series); set('bkeSeriesNum', b.series_number);
        set('bkeUniverse', b.universe); set('bkeGenre', b.genre); set('bkeDate', b.date_published);
        set('bkePages', b.page_count); set('bkeWords', b.word_count); set('bkeIsbn', b.isbn_id);
        set('bkeCoverUrl', '');
        bkeRenderCover();
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
        const fallback = `<div class="bke-cover-empty">No cover</div>`;
        const img = src => `<img src="${src}" alt="" style="width:100%;height:100%;object-fit:cover;" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">${fallback}`;
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
    async function _bkeSaveCore() {
        const id = document.getElementById('bkeId').value;
        const v = id2 => { const x = document.getElementById(id2).value.trim(); return x === '' ? null : x; };
        const num = (id2, fn) => { const x = document.getElementById(id2).value; return x === '' ? null : fn(x); };
        const data = {
            title: document.getElementById('bkeTitle').value.trim(),
            author_name_first: v('bkeAuthorFirst'), author_name_second: v('bkeAuthorLast'),
            series: v('bkeSeries'), universe: v('bkeUniverse'), genre: v('bkeGenre'),
            series_number: num('bkeSeriesNum', parseFloat),
            date_published: document.getElementById('bkeDate').value || null,
            page_count: num('bkePages', parseInt), word_count: num('bkeWords', parseInt),
            isbn_id: num('bkeIsbn', parseInt),
        };
        if (bkeIsNew) return _bkeCreate(data);
        try {
            await api('/books/' + id, { method: 'PUT', data });
            toast('Book updated', 'success');
            const after = hooks().afterSave;
            if (after) after(parseInt(id, 10), data, bkeBook);
            return parseInt(id, 10);
        } catch (e) { toast('Save failed', 'danger'); return null; }
    }

    // Create a new book (#117): POST the fields, then — because cover endpoints
    // need the id — upload the buffered cover pick against the returned id.
    async function _bkeCreate(data) {
        if (!data.title) { toast('Title is required', 'warning'); return null; }
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

    // Expose the handlers referenced by inline onclick= in the modal markup.
    Object.assign(window, {
        bkeOpen, bkeOpenNew, bkeSave, bkeSaveAndNext, bkeDelete,
        bkeUploadFile, bkeCoverFromUrl, bkeRemoveCover, bkeLoadLists, wireBookEdit: wireAutocomplete,
    });

    document.addEventListener('DOMContentLoaded', () => { wireAutocomplete(); bkeLoadLists(); });
})();
