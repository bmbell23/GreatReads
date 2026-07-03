// Shared bulk-select / edit / delete controller (#162). Keyed on RAW book ids so any
// page can reuse it — the page provides hooks; this module owns the selection state,
// the shared bulk-edit modal (_bulk_edit_modal.html), and the apply/delete calls.
//
// A page wires it up once:
//   grBulkInit({ loadedIds, reRender, afterEdit, afterDelete })
//     loadedIds()            -> array of the book ids currently rendered/selectable
//     reRender()             -> re-render the grid (checkbox state reads grBulkHas)
//     afterEdit(ids, patch)  -> apply the scalar patch to those books + refresh
//     afterDelete(ids)       -> drop those books from the view + refresh
// Card markup asks grBulkActive()/grBulkHas(id); a select-mode button calls
// grBulkToggleMode(); the bulk bar buttons call grBulk* handlers by id.
(function () {
    const $ = id => document.getElementById(id);
    const esc = s => (s || '').replace(/[&<>"]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
    const api = (p, o) => GreatReads.apiCall(p, o);
    const toast = (m, t) => GreatReads.showToast && GreatReads.showToast(m, t);

    let active = false;
    const sel = new Set();
    let hooks = {};
    let genres = [];

    function updateBar() {
        const n = sel.size;
        const c = $('grBulkCount'); if (c) c.textContent = n;
        const e = $('grBulkEditBtn'); if (e) e.disabled = n === 0;
        const d = $('grBulkDeleteBtn'); if (d) d.disabled = n === 0;
        const en = $('grBulkEnrichBtn'); if (en) en.disabled = n === 0;
    }
    function setModeUI(on) {
        const btn = $('grBulkModeBtn');
        if (btn) { btn.classList.toggle('btn-primary', on); btn.classList.toggle('btn-outline-secondary', !on); }
        const bar = $('grBulkBar');
        if (bar) { bar.classList.toggle('d-none', !on); bar.classList.toggle('d-flex', on); }
    }
    function exitMode() {
        sel.clear(); active = false; setModeUI(false); updateBar();
        if (hooks.reRender) hooks.reRender();
    }

    window.grBulkInit = h => { hooks = h || {}; };
    window.grBulkActive = () => active;
    window.grBulkHas = id => sel.has(id);

    window.grBulkToggleMode = function () {
        active = !active;
        if (!active) sel.clear();
        setModeUI(active); updateBar();
        if (hooks.reRender) hooks.reRender();
    };
    window.grBulkToggle = function (id) {
        if (sel.has(id)) sel.delete(id); else sel.add(id);
        updateBar();
    };
    window.grBulkSelectAllLoaded = function () {
        (hooks.loadedIds ? hooks.loadedIds() : []).forEach(id => sel.add(id));
        updateBar(); if (hooks.reRender) hooks.reRender();
    };
    window.grBulkClearSel = function () {
        sel.clear(); updateBar(); if (hooks.reRender) hooks.reRender();
    };

    // ---- Genres pills (mirrors the Store bulk, #160) ----
    function renderGenres() {
        const box = $('grBulkGenres'), input = $('grBulkGenreInput');
        if (!box || !input) return;
        box.querySelectorAll('.bke-genre-chip').forEach(c => c.remove());
        genres.forEach(name => {
            const chip = document.createElement('span');
            chip.className = 'bke-genre-chip';
            chip.innerHTML = '<span></span><button type="button" title="Remove">&times;</button>';
            chip.querySelector('span').textContent = name;
            chip.querySelector('button').addEventListener('click', () => {
                genres = genres.filter(g => g.toLowerCase() !== name.toLowerCase()); renderGenres();
            });
            box.insertBefore(chip, input);
        });
    }
    function addGenre(name) {
        name = String(name || '').trim(); if (!name) return;
        if (!genres.some(g => g.toLowerCase() === name.toLowerCase())) { genres.push(name); renderGenres(); }
    }
    window.grBulkGenreKey = function (e) {
        const input = e.target;
        if (e.key === 'Enter' || e.key === ',') { if (input.value.trim()) { addGenre(input.value); input.value = ''; e.preventDefault(); } }
        else if (e.key === 'Backspace' && !input.value && genres.length) { genres.pop(); renderGenres(); e.preventDefault(); }
    };

    function wireAutocomplete() {
        // Reuse Edit Book's WebView-safe autocomplete + shared genre/series/… vocab
        // (fixes the native-datalist quirk where Enter didn't compile a pill, #1/#3).
        if (typeof grAttachAutocomplete !== 'function') return;
        if (window.bkeLoadLists) window.bkeLoadLists();   // ensure the vocab is loaded
        grAttachAutocomplete('grBulkGenreInput', 'genre', addGenre);
        grAttachAutocomplete('grBulkAuthorLast', 'last');
        grAttachAutocomplete('grBulkSeries', 'series');
        grAttachAutocomplete('grBulkUniverse', 'universe');
    }

    window.grBulkOpen = async function () {
        if (!sel.size) return;
        const ids = [...sel];
        ['grBulkAuthorFirst', 'grBulkAuthorLast', 'grBulkNarratorFirst', 'grBulkNarratorLast',
         'grBulkSeries', 'grBulkSeriesNum', 'grBulkUniverse', 'grBulkGenreInput']
            .forEach(id => { const el = $(id); if (el) el.value = ''; });
        ['grBulkAddAuthorsBox', 'grBulkAddNarratorsBox'].forEach(b => { const el = $(b); if (el) el.innerHTML = ''; });
        genres = []; renderGenres();
        wireAutocomplete();
        const addR = $('grBulkModeAdd'); if (addR) addR.checked = true;
        $('grBulkModalCount').textContent = `(${ids.length} book${ids.length > 1 ? 's' : ''})`;
        $('grBulkGenreCommon').textContent = '';
        bootstrap.Modal.getOrCreateInstance($('grBulkModal')).show();
        try {
            const summary = await api('/books/genres-summary', { method: 'POST', data: { ids } });
            const common = summary.common || [], partial = summary.partial || [];
            const parts = [];
            if (common.length) parts.push('Shared by all: ' + common.map(esc).join(', '));
            if (partial.length) parts.push('On some: ' + partial.map(esc).join(', '));
            $('grBulkGenreCommon').textContent = parts.join(' · ') || 'No genres on the selected books yet.';
        } catch (e) { $('grBulkGenreCommon').textContent = ''; }
    };

    window.grBulkApply = async function () {
        const ids = [...sel];
        if (!ids.length) return;
        const v = id => { const el = $(id); return (el && el.value.trim()) ? el.value.trim() : null; };
        // Field updates (series/universe/genres) go through bulk-update. Author/narrator
        // are contributor ops so the primary + additional stay in sync (#192).
        const payload = { ids };
        const s = v('grBulkSeries'); if (s) payload.series = s;
        const snEl = $('grBulkSeriesNum'); if (snEl && snEl.value !== '') payload.series_number = parseFloat(snEl.value);
        const u = v('grBulkUniverse'); if (u) payload.universe = u;
        const mode = (document.querySelector('input[name=grBulkMode]:checked') || {}).value || 'add';
        if (mode === 'replace' || genres.length) { payload.genres = genres.slice(); payload.genres_mode = mode; }
        // Primary author/narrator → set-primary; the +Add rows → bulk-add (additional).
        const setPrimary = [];
        if (v('grBulkAuthorFirst') || v('grBulkAuthorLast'))
            setPrimary.push({ role: 'author', first: v('grBulkAuthorFirst'), last: v('grBulkAuthorLast') });
        if (v('grBulkNarratorFirst') || v('grBulkNarratorLast'))
            setPrimary.push({ role: 'narrator', first: v('grBulkNarratorFirst'), last: v('grBulkNarratorLast') });
        const rowVals = boxId => [...($(boxId)?.querySelectorAll('.bke-contrib-row') || [])]
            .map(rw => ({ first: rw.querySelector('.bke-cf').value.trim(), last: rw.querySelector('.bke-cl').value.trim() }))
            .filter(x => x.first || x.last);
        const adds = [
            ...rowVals('grBulkAddAuthorsBox').map(x => ({ role: 'author', ...x })),
            ...rowVals('grBulkAddNarratorsBox').map(x => ({ role: 'narrator', ...x })),
        ];
        const hasFieldUpdates = Object.keys(payload).length > 1;
        if (!hasFieldUpdates && !setPrimary.length && !adds.length) { toast('Fill in at least one field to apply.', 'info'); return; }
        const msgs = [];
        let r = { updated: 0 };
        if (hasFieldUpdates) {
            try { r = await api('/books/bulk-update', { method: 'POST', data: payload }); }
            catch (e) { toast('Bulk update failed', 'danger'); return; }
            msgs.push(`Updated ${r.updated} book${r.updated === 1 ? '' : 's'}`);
        }
        for (const sp of setPrimary) {
            try { const rr = await api('/books/contributors/bulk-set-primary', { method: 'POST', data: { book_ids: ids, ...sp } });
                msgs.push(`Set primary ${sp.role} on ${rr.updated}`); } catch (e) { toast(`Could not set ${sp.role}`, 'danger'); }
        }
        for (const c of adds) {
            try { const cr = await api('/books/contributors/bulk-add', { method: 'POST', data: { book_ids: ids, ...c } });
                msgs.push(`Added ${c.role} to ${cr.added}${cr.skipped ? ` (+${cr.skipped} had it)` : ''}`); } catch (e) { toast(`Could not add ${c.role}`, 'danger'); }
        }
        toast(msgs.join(' · ') || 'Nothing to apply', 'success');
        bootstrap.Modal.getInstance($('grBulkModal'))?.hide();
        const patch = {};
        if (payload.series !== undefined) patch.series = payload.series;
        if (payload.series_number !== undefined) patch.series_number = payload.series_number;
        if (payload.universe !== undefined) patch.universe = payload.universe;
        const pa = setPrimary.find(x => x.role === 'author');
        if (pa && pa.first && pa.last) patch.author = pa.first + ' ' + pa.last;
        if (payload.genres_mode === 'replace' && payload.genres.length) patch.genre = payload.genres[0];
        if (hooks.afterEdit) hooks.afterEdit(ids, patch);
        exitMode();
    };

    // Additional author/narrator rows — revealed by the "+ Add additional…" buttons (#192).
    window.grBulkAddContribRow = function (role) {
        const box = $(role === 'author' ? 'grBulkAddAuthorsBox' : 'grBulkAddNarratorsBox');
        if (!box) return;
        const row = document.createElement('div');
        row.className = 'd-flex gap-2 mt-1 bke-contrib-row';
        row.innerHTML =
            '<input class="form-control form-control-sm bke-cf" placeholder="First name" autocomplete="off">' +
            '<input class="form-control form-control-sm bke-cl" placeholder="Last name" autocomplete="off">' +
            '<button type="button" class="btn btn-sm btn-outline-secondary" title="Remove" onclick="this.parentNode.remove()">&times;</button>';
        box.appendChild(row);
    };

    window.grBulkFetchMeta = async function () {
        const ids = [...sel];
        if (!ids.length) return;
        toast(`Fetching metadata for ${ids.length} book${ids.length > 1 ? 's' : ''}…`, 'info');
        let r;
        try { r = await api('/books/bulk-enrich', { method: 'POST', data: { ids } }); }
        catch (e) { toast('Fetch metadata failed', 'danger'); return; }
        const u = (r && r.updated) || 0, f = (r && r.fields_filled) || 0;
        toast(u ? `Filled ${f} field${f === 1 ? '' : 's'} across ${u} book${u === 1 ? '' : 's'}` : 'Nothing new found for the selection', u ? 'success' : 'info');
        if (hooks.afterEnrich) hooks.afterEnrich(ids); else if (hooks.reRender) hooks.reRender();
        exitMode();
    };

    window.grBulkDelete = async function () {
        const ids = [...sel];
        if (!ids.length) return;
        if (!confirm(`Delete ${ids.length} selected book${ids.length > 1 ? 's' : ''}? This cannot be undone.`)) return;
        let r;
        try { r = await api('/books/bulk-delete', { method: 'POST', data: { ids } }); }
        catch (e) { toast('Bulk delete failed', 'danger'); return; }
        const n = (r && r.deleted != null) ? r.deleted : ids.length;
        toast(`Deleted ${n} book${n === 1 ? '' : 's'}`, 'success');
        if (hooks.afterDelete) hooks.afterDelete(ids);
        exitMode();
    };
})();
