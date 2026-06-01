// Requests page — kanban-style list grouped by status. Single source of
// truth is the backend (/api/requests); we re-fetch after every mutation
// rather than maintain a parallel client-side cache.
//
// Each request item carries an optional `sections` array
//   [{ id, title, body: html }]
// rendered as tabs in the detail view. Older items with no sections show a
// single synthetic "Overview" tab built from the legacy `body` string.

const API = 'http://100.69.184.113:8091';
const STATUSES = ['Backlog', 'Requested', 'In Progress', 'Done'];
const STATUS_CLASS = { 'Backlog': 'status-Backlog', 'Requested': 'status-Requested',
                       'In Progress': 'status-InProgress', 'Done': 'status-Done' };
// Default section names a fresh request starts with. The user can rename,
// delete, or add more — these are just sensible starting points for a
// design-doc-style request.
const DEFAULT_SECTION_NAMES = [
    'Overview', 'UX', 'Technical', 'Implementation', 'Open Questions',
];

const $ = (sel) => document.querySelector(sel);
const esc = (s) => (s == null ? '' : String(s)).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtDate = (ms) => {
    if (!ms) return '';
    const d = new Date(ms);
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    return sameDay
        ? d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})
        : d.toLocaleDateString([], {month: 'short', day: 'numeric'});
};

// Crypto.randomUUID is available in modern Android WebView; fall back to a
// timestamp+random hybrid for older runtimes so we never collide on the
// client side (server doesn't care — it accepts whatever id we send).
const newId = () => (crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

// Minimal HTML sanitiser — strips <script>, inline event handlers, and
// javascript: URLs. We trust the server (single user) but still want to
// avoid an accidentally pasted snippet breaking the page.
function sanitizeHTML(html) {
    const tpl = document.createElement('template');
    tpl.innerHTML = String(html || '');
    tpl.content.querySelectorAll('script,style,iframe,object,embed').forEach(n => n.remove());
    tpl.content.querySelectorAll('*').forEach(el => {
        [...el.attributes].forEach(attr => {
            const n = attr.name.toLowerCase();
            const v = (attr.value || '').trim().toLowerCase();
            if (n.startsWith('on')) el.removeAttribute(attr.name);
            else if ((n === 'href' || n === 'src') && v.startsWith('javascript:'))
                el.removeAttribute(attr.name);
        });
    });
    return tpl.innerHTML;
}

let _items = [];
let _editingId = null;   // null = creating new, string = editing existing
let _draft = null;       // working copy of the item being edited (sections live here)
let _activeTab = 0;      // index into _draft.sections

async function loadAll() {
    try {
        const res = await fetch(`${API}/api/requests`, {cache: 'no-store'});
        const data = await res.json();
        _items = data.items || [];
    } catch (e) {
        console.error('Failed to load requests:', e);
        _items = [];
    }
    render();
}

function render() {
    const container = $('#container');
    if (_items.length === 0) {
        container.innerHTML = '<div class="empty">No requests yet. Tap + to add one.</div>';
        return;
    }
    const groups = {};
    STATUSES.forEach(s => groups[s] = []);
    _items.forEach(it => {
        const s = STATUSES.includes(it.status) ? it.status : 'Backlog';
        groups[s].push(it);
    });
    const html = STATUSES.map(status => {
        const items = groups[status];
        if (items.length === 0) return '';
        const cards = items.map(it => `
            <div class="req-card ${STATUS_CLASS[status]}" data-id="${esc(it.id)}">
                <div class="req-title">${esc(it.title)}</div>
                <div class="req-meta">
                    <span>${esc(fmtDate(it.updated))}</span>
                    ${it.comments && it.comments.length ? `<span>💬 ${it.comments.length}</span>` : ''}
                </div>
            </div>`).join('');
        return `<div class="group-header">${esc(status)} <span class="count">${items.length}</span></div>${cards}`;
    }).join('');
    container.innerHTML = html;
    container.querySelectorAll('.req-card').forEach(el => {
        el.addEventListener('click', () => openDetail(el.dataset.id));
    });
}

// Build the working-copy `_draft` we mutate while the overlay is open.
// Always ensures at least one section exists so the tab strip is never
// empty. Migrates legacy items by promoting `body` into a single section.
function buildDraft(item) {
    if (!item) {
        return {
            title: '', status: 'Backlog', comments: [],
            sections: [{ id: newId(), title: 'Overview', body: '' }],
        };
    }
    let sections = Array.isArray(item.sections) ? item.sections.slice() : [];
    if (sections.length === 0) {
        // Legacy item: synthesise an Overview section from the plain-text body.
        const legacy = (item.body || '').trim();
        const html = legacy ? `<p>${esc(legacy).replace(/\n/g, '<br>')}</p>` : '';
        sections = [{ id: newId(), title: 'Overview', body: html }];
    }
    return {
        title: item.title || '',
        status: item.status || 'Backlog',
        comments: item.comments || [],
        sections,
    };
}

function openDetail(id) {
    _editingId = id;
    const item = id ? _items.find(it => it.id === id) : null;
    const isNew = !item;
    _draft = buildDraft(item);
    _activeTab = 0;
    $('#detail-title').textContent = isNew ? 'New Request' : 'Edit Request';
    $('#detail-delete').style.display = isNew ? 'none' : '';
    const statusOpts = STATUSES.map(s =>
        `<option value="${s}"${s === _draft.status ? ' selected' : ''}>${s}</option>`).join('');
    const commentsHtml = (_draft.comments || []).map(c => `
        <div class="comment">
            <div class="c-meta"><span>${esc(c.author || 'user')}</span><span>${esc(fmtDate(c.ts))}</span></div>
            <div class="c-text">${esc(c.text)}</div>
        </div>`).join('');
    $('#detail-body').innerHTML = `
        <div class="field-label">Title</div>
        <input class="field-input" id="f-title" type="text" value="${esc(_draft.title)}" placeholder="Short summary">
        <div class="field-label">Status</div>
        <select class="field-select" id="f-status">${statusOpts}</select>
        <div class="field-label">Sections</div>
        <div class="tabs-bar" id="tabs-bar"></div>
        <div class="rt-toolbar" id="rt-toolbar"></div>
        <div class="rt-editor" id="rt-editor" contenteditable="true"
             data-placeholder="Start writing… use the toolbar above for formatting."></div>
        <div class="section-actions">
            <button class="mini-btn" id="sec-rename">✏️ Rename section</button>
            <button class="mini-btn danger" id="sec-delete">🗑️ Delete section</button>
        </div>
        <div class="row-buttons">
            <button class="btn btn-primary" id="f-save">${isNew ? 'Create' : 'Save'}</button>
        </div>
        ${isNew ? '' : `
            <div class="field-label">Comments (${(_draft.comments || []).length})</div>
            <div id="comments-list">${commentsHtml || '<div class="empty" style="padding:16px 0;">No comments yet.</div>'}</div>
            <div class="field-label">Add comment</div>
            <div class="comment-composer">
                <textarea class="field-textarea" id="f-comment" placeholder="Reply / iterate…"></textarea>
                <button class="btn btn-primary" id="f-comment-send">Post</button>
            </div>
        `}
    `;
    renderToolbar();
    renderTabs();
    showActiveSection();
    // Track dirty state on the active editor so switching tabs doesn't lose edits.
    $('#rt-editor').addEventListener('input', () => {
        if (_draft.sections[_activeTab]) {
            _draft.sections[_activeTab].body = $('#rt-editor').innerHTML;
        }
    });
    // Suggest a default name for the next section the user adds, cycling
    // through DEFAULT_SECTION_NAMES that aren't taken yet.
    $('#sec-rename').addEventListener('click', () => renameSection(_activeTab));
    $('#sec-delete').addEventListener('click', () => deleteSection(_activeTab));
    $('#f-save').addEventListener('click', saveDetail);
    if (!isNew) {
        $('#f-comment-send').addEventListener('click', postComment);
    }
    $('#detail-overlay').classList.add('open');
}

function closeDetail() {
    $('#detail-overlay').classList.remove('open');
    _editingId = null;
    _draft = null;
}

async function saveDetail() {
    const title = $('#f-title').value.trim();
    if (!title) { alert('Title is required.'); return; }
    // Capture the live editor's contents into the draft before serialising.
    if (_draft.sections[_activeTab]) {
        _draft.sections[_activeTab].body = $('#rt-editor').innerHTML;
    }
    const sections = _draft.sections.map(s => ({
        id: s.id, title: s.title, body: sanitizeHTML(s.body || ''),
    }));
    const status = $('#f-status').value;
    const payload = { title, status, sections };
    try {
        if (_editingId) {
            await fetch(`${API}/api/requests/${_editingId}`, {
                method: 'PUT', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
        } else {
            await fetch(`${API}/api/requests`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
        }
    } catch (e) { alert('Save failed: ' + e); return; }
    closeDetail();
    loadAll();
}

// ----- Tabs / sections -----

function renderTabs() {
    const bar = $('#tabs-bar');
    if (!bar) return;
    const tabsHtml = _draft.sections.map((s, i) => `
        <div class="tab${i === _activeTab ? ' active' : ''}" data-i="${i}">${esc(s.title)}</div>
    `).join('');
    bar.innerHTML = tabsHtml + `<div class="tab add-tab" id="tab-add" title="Add section">+</div>`;
    bar.querySelectorAll('.tab').forEach(el => {
        if (el.id === 'tab-add') {
            el.addEventListener('click', addSection);
            return;
        }
        el.addEventListener('click', () => switchTab(parseInt(el.dataset.i, 10)));
    });
}

function showActiveSection() {
    const sec = _draft.sections[_activeTab];
    const ed = $('#rt-editor');
    ed.innerHTML = sec ? (sec.body || '') : '';
}

// Commit the editor's current contents back to the draft, then switch.
// Without this, switching tabs would discard unsaved edits in the live editor.
function switchTab(i) {
    if (i === _activeTab) return;
    if (_draft.sections[_activeTab]) {
        _draft.sections[_activeTab].body = $('#rt-editor').innerHTML;
    }
    _activeTab = i;
    renderTabs();
    showActiveSection();
}

function addSection() {
    // Pick the next unused default name; if all are used, ask the user.
    const used = new Set(_draft.sections.map(s => s.title));
    let name = DEFAULT_SECTION_NAMES.find(n => !used.has(n));
    if (!name) {
        name = prompt('Section name:', 'New section');
        if (!name) return;
    }
    // Persist whatever's currently in the editor before adding.
    if (_draft.sections[_activeTab]) {
        _draft.sections[_activeTab].body = $('#rt-editor').innerHTML;
    }
    _draft.sections.push({ id: newId(), title: name, body: '' });
    _activeTab = _draft.sections.length - 1;
    renderTabs();
    showActiveSection();
}

function renameSection(i) {
    const cur = _draft.sections[i];
    if (!cur) return;
    const next = prompt('Rename section:', cur.title);
    if (!next || !next.trim()) return;
    cur.title = next.trim();
    renderTabs();
}

function deleteSection(i) {
    if (_draft.sections.length <= 1) {
        alert('At least one section is required.');
        return;
    }
    const cur = _draft.sections[i];
    if (!confirm(`Delete the "${cur.title}" section? Its content will be lost.`)) return;
    _draft.sections.splice(i, 1);
    if (_activeTab >= _draft.sections.length) _activeTab = _draft.sections.length - 1;
    renderTabs();
    showActiveSection();
}

// ----- Rich-text toolbar -----
// Uses document.execCommand — deprecated in the spec but the only
// no-dependency option that still works reliably in Android WebView. If
// it ever stops working we'd swap in a tiny editor lib, but for a
// single-user notes pane this is the smallest surface area that gets
// us bold/italic/headings/lists/links/code with zero build step.

const TOOLBAR_BUTTONS = [
    { cmd: 'bold',          html: '<b>B</b>',  title: 'Bold' },
    { cmd: 'italic',        html: '<i>I</i>',  title: 'Italic' },
    { cmd: 'underline',     html: '<u>U</u>',  title: 'Underline' },
    { sep: true },
    { block: 'H1', html: 'H1', title: 'Heading 1' },
    { block: 'H2', html: 'H2', title: 'Heading 2' },
    { block: 'H3', html: 'H3', title: 'Heading 3' },
    { block: 'P',  html: '¶',  title: 'Paragraph' },
    { sep: true },
    { cmd: 'insertUnorderedList', html: '•',  title: 'Bulleted list' },
    { cmd: 'insertOrderedList',   html: '1.', title: 'Numbered list' },
    { block: 'BLOCKQUOTE', html: '❝', title: 'Quote' },
    { sep: true },
    { custom: 'link',  html: '🔗', title: 'Insert link' },
    { custom: 'code',  html: '< >', title: 'Inline code' },
    { custom: 'pre',   html: '{ }', title: 'Code block' },
    { sep: true },
    { cmd: 'removeFormat', html: '⌫', title: 'Clear formatting' },
];

function renderToolbar() {
    const tb = $('#rt-toolbar');
    if (!tb) return;
    tb.innerHTML = TOOLBAR_BUTTONS.map((b, i) => {
        if (b.sep) return `<div class="sep"></div>`;
        return `<button type="button" data-i="${i}" title="${esc(b.title)}">${b.html}</button>`;
    }).join('');
    tb.querySelectorAll('button').forEach(btn => {
        // mousedown (not click) so focus stays in the editor and the
        // current selection isn't lost when the button is pressed.
        btn.addEventListener('mousedown', (e) => {
            e.preventDefault();
            const b = TOOLBAR_BUTTONS[parseInt(btn.dataset.i, 10)];
            applyToolbarAction(b);
        });
    });
}

function applyToolbarAction(b) {
    const ed = $('#rt-editor');
    ed.focus();
    if (b.cmd)   { document.execCommand(b.cmd, false, null); }
    else if (b.block) { document.execCommand('formatBlock', false, b.block); }
    else if (b.custom === 'link') {
        const url = prompt('Link URL:', 'https://');
        if (url) document.execCommand('createLink', false, url);
    }
    else if (b.custom === 'code') {
        // Wrap the current selection in <code>…</code>. execCommand has no
        // native equivalent, so do it manually via the Range API.
        const sel = window.getSelection();
        if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return;
        const range = sel.getRangeAt(0);
        const node = document.createElement('code');
        node.appendChild(range.extractContents());
        range.insertNode(node);
    }
    else if (b.custom === 'pre') {
        document.execCommand('formatBlock', false, 'PRE');
    }
    // Mirror the change back into the draft immediately so a tab switch
    // doesn't drop it (the `input` event already covers typing, but
    // execCommand on some WebView builds skips firing it).
    if (_draft && _draft.sections[_activeTab]) {
        _draft.sections[_activeTab].body = ed.innerHTML;
    }
}

async function postComment() {
    const text = $('#f-comment').value.trim();
    if (!text || !_editingId) return;
    try {
        const res = await fetch(`${API}/api/requests/${_editingId}/comments`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text})
        });
        const updated = await res.json();
        // Refresh in-memory item and re-render detail to show new comment.
        const i = _items.findIndex(it => it.id === _editingId);
        if (i >= 0) _items[i] = updated;
        openDetail(_editingId);
    } catch (e) { alert('Failed to post: ' + e); }
}

async function deleteDetail() {
    if (!_editingId) { closeDetail(); return; }
    if (!confirm('Delete this request? This cannot be undone.')) return;
    try {
        await fetch(`${API}/api/requests/${_editingId}`, {method: 'DELETE'});
    } catch (e) { alert('Delete failed: ' + e); return; }
    closeDetail();
    loadAll();
}

$('#back-btn').addEventListener('click', () => { window.location.href = '/'; });
$('#new-btn').addEventListener('click', () => openDetail(null));
$('#detail-back').addEventListener('click', closeDetail);
$('#detail-delete').addEventListener('click', deleteDetail);

loadAll();
