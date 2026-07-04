// Active-book marker (#198). The Android app hard-loads "/" whenever the OS
// recreates the activity (lock screen, memory pressure), which used to dump
// the user on Home even though a book was open. reader.html / player.html
// keep localStorage['gr.activeBook'] = {url, ts} pointing at themselves, and
// serve.py's "/" bootstrap page redirects back into the book while the marker
// exists and is fresh. The marker must therefore be CLEARED on any *real*
// navigation away (back to the library, closing the book) and kept alive on
// backgrounding (screen lock, app switch) — process death fires no events,
// which is exactly why backgrounding must refresh rather than clear.
(function () {
    var KEY = 'gr.activeBook';
    // player.html embeds reader.html in an <iframe> for dual-format overlays;
    // that embedded reader must neither claim the marker (the PLAYER is the
    // open page) nor clear it when the overlay closes. localStorage is shared,
    // so every op no-ops unless we're the top-level page.
    var isTop = true;
    try { isTop = (window.self === window.top); } catch (_) { isTop = false; }
    // Set once a real navigation away has begun, so the later
    // visibilitychange:hidden (Chrome fires it AFTER pagehide during unload)
    // can't resurrect the marker that pagehide just cleared.
    var navigatedAway = false;

    window.ActiveBook = {
        KEY: KEY,

        // Stamp this page as the open book. Call on boot and from periodic
        // progress saves so ts stays fresh for the bootstrap's staleness check.
        set: function () {
            if (!isTop || navigatedAway) return;
            try {
                localStorage.setItem(KEY, JSON.stringify({
                    url: location.pathname + location.search,
                    ts: Date.now()
                }));
            } catch (_) {}
        },

        clear: function () {
            if (!isTop) return;
            navigatedAway = true;
            try { localStorage.removeItem(KEY); } catch (_) {}
        },

        // Wire the standard lifecycle for a book page (reader/player):
        //  - pagehide without `persisted` = the page is really being left
        //    (back to library, new navigation) → clear.
        //  - visibilitychange:hidden = backgrounded (lock/app switch) → refresh
        //    so a subsequent silent process kill still finds a fresh marker.
        trackPage: function () {
            if (!isTop) return;
            this.set();
            // Back-exit trap (#208): after a cold rehydration this book page is
            // the WebView's ONLY history entry (the "/" bootstrap navigated with
            // location.replace), so hardware/gesture back exits the app — which
            // then relaunches, rehydrates back into the book, and traps the
            // user. Seed a synthetic "exit to Home" entry beneath us: popping to
            // it clears the marker and lands on Home instead of exiting. The
            // reader's overlay back-trap stacks its own sentinel on TOP of ours
            // and its popstate handler no-ops when no overlay is open, so the
            // two coexist.
            if (history.length <= 1) {
                try {
                    history.replaceState({ grExitHome: true }, '');
                    history.pushState({ grBook: true }, '');
                    window.addEventListener('popstate', function () {
                        if (history.state && history.state.grExitHome) {
                            window.ActiveBook.clear();
                            location.replace('/greatreads/');
                        }
                    });
                } catch (_) {}
            }
            window.addEventListener('pagehide', function (e) {
                if (!e.persisted) window.ActiveBook.clear();
            });
            document.addEventListener('visibilitychange', function () {
                if (document.visibilityState === 'hidden') window.ActiveBook.set();
            });
        }
    };
})();
