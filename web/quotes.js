// Shared "loading quotes" screen (#55). While an ebook paginates or an
// audiobook session spins up, show a random highlight the user has saved —
// quote in italics with book / chapter / author below — instead of a bare
// "Loading…" string. Used by both reader.html (.loading) and player.html
// (#overlay-msg).
//
// Design notes:
//  - Reads a localStorage cache FIRST so a quote paints instantly (even
//    offline / before any network), then refreshes the cache in the background
//    from /api/highlights?type=highlight for next time.
//  - Rotates every few seconds with a gentle cross-fade while loading lasts.
//  - Falls back to a small set of boilerplate literary quotes ONLY when the
//    user has no highlights of their own (cache empty AND fetch empty).
//  - Theme-agnostic: the quote inherits the host element's color; meta uses
//    opacity so it reads correctly on both the reader (light) and player (dark).
//  - Auto-stops if the host gets hidden or its content is replaced (e.g. an
//    error message), so callers don't have to stop() from every code path.
(function () {
    'use strict';

    var CACHE_KEY = 'gr.quotes.cache';
    var MAX_CACHE = 200;
    var ROTATE_MS = 7000;
    var FADE_MS = 450;

    // Shown only when the user has zero highlights. Real attributions; rendered
    // WITHOUT the "From your highlights" caption since they aren't the user's.
    var BOILERPLATE = [
        { text: 'A reader lives a thousand lives before he dies. The man who never reads lives only one.', author: 'George R.R. Martin', book: 'A Dance with Dragons' },
        { text: 'Until I feared I would lose it, I never loved to read. One does not love breathing.', author: 'Harper Lee', book: 'To Kill a Mockingbird' },
        { text: 'Books are a uniquely portable magic.', author: 'Stephen King', book: 'On Writing' },
        { text: 'That is part of the beauty of all literature. You discover that your longings are universal longings, that you’re not lonely and isolated from anyone. You belong.', author: 'F. Scott Fitzgerald' },
        { text: 'There is no friend as loyal as a book.', author: 'Ernest Hemingway' },
        { text: 'I have always imagined that Paradise will be a kind of library.', author: 'Jorge Luis Borges' },
        { text: 'We read to know we are not alone.', author: 'C.S. Lewis' },
        { text: 'A word after a word after a word is power.', author: 'Margaret Atwood' }
    ];

    var styleInjected = false;
    function injectStyle() {
        if (styleInjected) return;
        styleInjected = true;
        var css = ''
            + '.grq{max-width:560px;width:86vw;margin:0 auto;padding:8px 4px;text-align:center;'
            + 'opacity:0;transition:opacity ' + FADE_MS + 'ms ease;'
            + 'font-family:Georgia,"Times New Roman",serif;line-height:1.5;}'
            + '.grq.grq-in{opacity:1;}'
            + '.grq-cap{font-family:-apple-system,system-ui,sans-serif;font-style:normal;'
            + 'font-size:11px;letter-spacing:.14em;text-transform:uppercase;'
            + 'opacity:.5;margin-bottom:18px;}'
            + '.grq-text{font-style:italic;font-size:20px;margin:0 0 18px;'
            + 'display:-webkit-box;-webkit-line-clamp:9;-webkit-box-orient:vertical;overflow:hidden;}'
            + '.grq-meta{font-family:-apple-system,system-ui,sans-serif;font-style:normal;'
            + 'font-size:13px;opacity:.7;line-height:1.7;}'
            + '.grq-book{font-weight:600;}'
            + '.grq-chapter{opacity:.85;}'
            + '.grq-author{display:block;opacity:.7;margin-top:2px;}'
            + '.grq-sep{opacity:.5;margin:0 7px;}';
        var el = document.createElement('style');
        el.id = 'grq-style';
        el.textContent = css;
        (document.head || document.documentElement).appendChild(el);
    }

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function readCache() {
        try {
            var raw = localStorage.getItem(CACHE_KEY);
            var arr = raw ? JSON.parse(raw) : null;
            return Array.isArray(arr) ? arr : [];
        } catch (_) { return []; }
    }

    function refreshCache(apiUrl, cb) {
        try {
            fetch(apiUrl + '/highlights?type=highlight')
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (!data || !Array.isArray(data.items)) return;
                    var out = [];
                    for (var i = 0; i < data.items.length; i++) {
                        var it = data.items[i];
                        var t = (it && it.text ? String(it.text) : '').trim();
                        if (!t) continue;
                        out.push({
                            text: t,
                            book: (it.bookTitle || '').trim(),
                            author: (it.bookAuthor || '').trim(),
                            chapter: (it.chapter || '').trim()
                        });
                    }
                    if (out.length > MAX_CACHE) out = out.slice(0, MAX_CACHE);
                    try { localStorage.setItem(CACHE_KEY, JSON.stringify(out)); } catch (_) {}
                    if (cb) cb(out);
                })
                .catch(function () {});
        } catch (_) {}
    }

    function shuffle(a) {
        a = a.slice();
        for (var i = a.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1));
            var t = a[i]; a[i] = a[j]; a[j] = t;
        }
        return a;
    }

    function quoteHTML(q, isOwn) {
        var meta = '';
        var parts = [];
        if (q.book) parts.push('<span class="grq-book">' + esc(q.book) + '</span>');
        if (q.chapter) parts.push('<span class="grq-chapter">' + esc(q.chapter) + '</span>');
        var line1 = parts.join('<span class="grq-sep">&middot;</span>');
        var author = q.author ? '<span class="grq-author">' + esc(q.author) + '</span>' : '';
        if (line1 || author) meta = '<div class="grq-meta">' + line1 + author + '</div>';
        var cap = isOwn ? '<div class="grq-cap">&#10022; From your highlights</div>' : '';
        return '<div class="grq">' + cap
            + '<blockquote class="grq-text">&ldquo;' + esc(q.text) + '&rdquo;</blockquote>'
            + meta + '</div>';
    }

    // Module state — only one loading screen is ever live at a time.
    var hostEl = null;
    var timer = null;
    var pool = [];
    var poolIsOwn = false;
    var idx = 0;

    function stillValid() {
        // Auto-stop if the host vanished, got hidden, or its content was
        // replaced by something other than our quote (e.g. an error message).
        return hostEl && hostEl.isConnected
            && hostEl.style.display !== 'none'
            && !hostEl.classList.contains('hidden')
            && hostEl.querySelector('.grq');
    }

    function paint(q) {
        if (!hostEl) return;
        hostEl.innerHTML = quoteHTML(q, poolIsOwn);
        // Next frame so the fade-in transition runs.
        requestAnimationFrame(function () {
            var node = hostEl && hostEl.querySelector('.grq');
            if (node) node.classList.add('grq-in');
        });
    }

    function rotate() {
        if (!stillValid()) { stop(); return; }
        if (!pool.length) return;
        var node = hostEl.querySelector('.grq');
        idx = (idx + 1) % pool.length;
        var next = pool[idx];
        if (node) {
            node.classList.remove('grq-in'); // fade out
            setTimeout(function () { if (stillValid()) paint(next); }, FADE_MS);
        } else {
            paint(next);
        }
    }

    function setPool(list, isOwn) {
        if (!list || !list.length) return;
        pool = shuffle(list);
        poolIsOwn = !!isOwn;
        idx = 0;
        if (hostEl) paint(pool[0]);
    }

    function start(el, apiUrl) {
        if (!el) return;
        // Idempotent: re-starting on the same already-running host is a no-op so
        // repeated loadPart()/loading calls don't reset the rotation.
        if (hostEl === el && timer) return;
        stop();
        injectStyle();
        hostEl = el;
        hostEl.classList.remove('hidden');
        hostEl.style.display = '';

        var cached = readCache();
        if (cached.length) {
            setPool(cached, true);
        } else {
            setPool(BOILERPLATE, false);
        }

        // Refresh in the background; if it's the first-ever load (we showed
        // boilerplate) and real highlights come back, swap to them.
        refreshCache(apiUrl, function (fresh) {
            if (fresh && fresh.length && !poolIsOwn) setPool(fresh, true);
        });

        timer = setInterval(rotate, ROTATE_MS);
    }

    function stop() {
        if (timer) { clearInterval(timer); timer = null; }
        hostEl = null;
        pool = [];
        idx = 0;
    }

    window.GreatReadsQuotes = { start: start, stop: stop };
})();
