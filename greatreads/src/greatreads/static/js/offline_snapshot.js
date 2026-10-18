// Offline helpers for the Android shell (#275, #361).
//
// Offline Home is no longer rebuilt from an IndexedDB snapshot: the APK's native
// write-through cache (MainActivity.OfflineShellWebViewClient) replays the real
// server-rendered Home. What remains here is best-effort and off the critical
// path: ask Android not to evict cached books, and load the diagnostics helper.
(function () {
    'use strict';

    // #361: ask Android not to evict cached books/covers (best effort, only if
    // not already granted).
    try {
        if (navigator.storage && navigator.storage.persist) {
            navigator.storage.persisted().then(function (p) {
                if (!p) navigator.storage.persist().catch(function () {});
            }).catch(function () {});
        }
    } catch (_) {}

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
})();
