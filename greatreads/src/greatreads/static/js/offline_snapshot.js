// Offline Home snapshot (#275).
//
// The APK is a WebView pinned to a remote host, so with no signal there is no
// server to render Home from. Every online visit therefore leaves behind enough
// of Home to rebuild it locally: the in-progress readings and their covers, in
// the SAME IndexedDB (EreaderDB, origin :8090) that already holds cached books,
// covers and audiobooks. `web/offline-home.html` — bundled into the APK — reads
// this snapshot when the host is unreachable.
//
// Everything here is best-effort and off the critical path: a failure must never
// affect the online page the user is actually looking at.
(function () {
    'use strict';

    var SNAPSHOT_KEY = 'offline:home';
    var REFRESH_MS = 5 * 60 * 1000;      // don't re-snapshot on every navigation
    var COVER_LIMIT = 24;                // in-progress lists are small; cap anyway

    // Diagnostics live in web/gr-diag.js at the site root (served by :8090, and
    // bundled in the APK). Load it if it's reachable — GreatReads pages are always
    // served through the :8090 proxy in real use; opening :8092 directly just
    // means no diagnostics, which is fine.
    if (!window.GRDiag) {
        try {
            var s = document.createElement('script');
            s.src = '/gr-diag.js';
            s.async = true;
            document.head.appendChild(s);
        } catch (_) {}
    }

    function openDB() {
        return new Promise(function (resolve) {
            try {
                // Same schema the reader owns (EreaderDB v3). Open WITHOUT a version
                // so we never trigger an upgrade from this side — if the stores are
                // missing, the reader hasn't run yet and there is nothing to write.
                var req = indexedDB.open('EreaderDB');
                req.onsuccess = function () { resolve(req.result || null); };
                req.onerror = function () { resolve(null); };
                req.onblocked = function () { resolve(null); };
            } catch (_) { resolve(null); }
        });
    }

    function idbPut(db, store, value) {
        return new Promise(function (resolve) {
            if (!db || !db.objectStoreNames.contains(store)) return resolve(false);
            try {
                var tx = db.transaction([store], 'readwrite');
                tx.objectStore(store).put(value);
                tx.oncomplete = function () { resolve(true); };
                tx.onerror = function () { resolve(false); };
                tx.onabort = function () { resolve(false); };
            } catch (_) { resolve(false); }
        });
    }

    function idbGet(db, store, key) {
        return new Promise(function (resolve) {
            if (!db || !db.objectStoreNames.contains(store)) return resolve(null);
            try {
                var tx = db.transaction([store], 'readonly');
                var req = tx.objectStore(store).get(key);
                req.onsuccess = function () { resolve(req.result || null); };
                req.onerror = function () { resolve(null); };
            } catch (_) { resolve(null); }
        });
    }

    // One in-progress reading, reduced to what an offline Home card needs —
    // including the ids the reader/player are opened with (#275).
    function toCard(r) {
        var b = r.book || {};
        var base = window.APP_BASE_PATH || '';
        return {
            readingId: r.id,
            bookId: b.id,
            title: b.title || 'Untitled',
            author: b.author || '',
            series: b.series || '',
            seriesNumber: b.series_number || null,
            media: r.media || '',
            percent: (typeof r.current_percent === 'number') ? r.current_percent : null,
            calibreId: b.calibre_id || null,
            absId: b.abs_id || null,
            coverUrl: b.cover ? (base + '/static/covers/' + b.id + '.jpg?v=' + (b.cover_version || 0)) : null,
        };
    }

    // Store each cover as a blob keyed by its URL — the same convention the reader
    // grid uses, so a cover cached by either surface serves both.
    function cacheCovers(db, cards) {
        var urls = cards.map(function (c) { return c.coverUrl; })
                        .filter(Boolean).slice(0, COVER_LIMIT);
        return Promise.all(urls.map(function (url) {
            return idbGet(db, 'covers', url).then(function (hit) {
                if (hit && hit.blob) return false;                    // already cached
                return fetch(url).then(function (r) {
                    if (!r.ok) return false;
                    return r.blob().then(function (blob) {
                        return idbPut(db, 'covers', { id: url, blob: blob, cachedAt: Date.now() });
                    });
                }).catch(function () { return false; });
            });
        })).then(function (results) { return results.filter(Boolean).length; });
    }

    async function snapshot() {
        if (navigator.onLine === false) return;
        var db = await openDB();
        if (!db) return;

        var prev = await idbGet(db, 'cacheMeta', SNAPSHOT_KEY);
        if (prev && prev.ts && (Date.now() - prev.ts) < REFRESH_MS) return;

        var base = (window.APP_BASE_PATH || '') + '/api';
        var res = await fetch(base + '/readings/?status=in_progress&limit=100');
        if (!res.ok) return;
        var readings = await res.json();
        if (!Array.isArray(readings)) return;

        var cards = readings.map(toCard);
        await idbPut(db, 'cacheMeta', { id: SNAPSHOT_KEY, ts: Date.now(), books: cards });
        var covers = await cacheCovers(db, cards);
        if (window.GRDiag) {
            window.GRDiag.log('offline_snapshot', { books: cards.length, covers_cached: covers });
        }
    }

    // After load, and out of the way of first paint.
    if (document.readyState === 'complete') setTimeout(snapshot, 2000);
    else window.addEventListener('load', function () { setTimeout(snapshot, 2000); });
})();
