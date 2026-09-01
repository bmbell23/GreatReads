// Device diagnostics (#275) — the phone has no console and no adb, and the bugs
// that matter (offline launch, slow opens) happen while it is cut off. So every
// page queues small events into localStorage and flushes them to the server's
// activity log (#184) whenever it can reach the host. /logs then shows what the
// device actually did, offline stretches included.
//
// Rules: never throw, never block a page, never grow without bound.
(function (global) {
    'use strict';
    var API = 'http://100.69.184.113:8092/api';
    var KEY = 'gr.diag.queue';
    var MAX = 200;            // ring buffer — a long camping trip must not fill storage
    var BATCH = 50;           // matches the server's per-POST cap

    function read() {
        try { var q = JSON.parse(localStorage.getItem(KEY) || '[]'); return Array.isArray(q) ? q : []; }
        catch (_) { return []; }
    }
    function write(q) {
        try { localStorage.setItem(KEY, JSON.stringify(q.slice(-MAX))); } catch (_) {}
    }

    // Record one event. `detail` is a small flat object; keep it cheap — this can
    // run on a page that is already struggling.
    function log(event, detail, level, title) {
        try {
            var q = read();
            q.push({ event: String(event), detail: detail || {}, level: level || 'info',
                     title: title || null, at: new Date().toISOString() });
            write(q);
        } catch (_) {}
    }

    // Ship what we have. Only removes the events it actually delivered, so a
    // failed flush (still offline, host down) simply leaves them queued.
    function flush() {
        var q = read();
        if (!q.length) return Promise.resolve(0);
        var batch = q.slice(0, BATCH);
        return fetch(API + '/client-events', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ events: batch }),
        }).then(function (r) {
            if (!r.ok) return 0;
            write(read().slice(batch.length));
            return batch.length;
        }).catch(function () { return 0; });
    }

    // Flush on load and whenever connectivity returns. `online` fires on the
    // OS-level transition, which is exactly the reconnect we care about.
    function autoFlush() {
        try {
            if (navigator.onLine !== false) setTimeout(flush, 1500);
            global.addEventListener('online', function () { setTimeout(flush, 1000); });
        } catch (_) {}
    }

    global.GRDiag = { log: log, flush: flush, queued: function () { return read().length; } };
    autoFlush();
})(window);
