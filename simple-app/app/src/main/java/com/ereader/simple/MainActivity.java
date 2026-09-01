package com.ereader.simple;

import android.app.Activity;
import android.content.res.Configuration;
import android.os.Bundle;
import android.webkit.WebView;
import android.webkit.WebSettings;
import android.webkit.WebViewClient;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebResourceError;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.content.Intent;
import android.net.Uri;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import java.io.InputStream;
import java.io.IOException;
import android.app.DownloadManager;
import android.os.Environment;
import android.view.ActionMode;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.graphics.Color;
import android.content.pm.PackageManager;
import java.lang.ref.WeakReference;

public class MainActivity extends Activity {
    private WebView webView;

    // #210: ONE WebView for the app's lifetime. Folding to the cover screen
    // destroys + recreates the Activity even with configChanges declared (a
    // physical display switch), and a recreated activity used to build a NEW
    // WebView and cold-load "/" — a loading screen, a reloaded page (killed
    // reading-session timers), and a ghost audio player (#207's symptom via a
    // second path). Instead the WebView is created once, on a
    // MutableContextWrapper, and every subsequent activity just re-points the
    // wrapper at itself and re-attaches the view: the live page (timer, audio,
    // scroll) carries over untouched. Everything the retained WebView's
    // clients/bridge need from an Activity is routed through sRef (the CURRENT
    // activity) or the application context — never a captured instance.
    private static WebView sWebView;
    private static android.content.MutableContextWrapper sWebCtx;

    // Pending callback for a WebView <input type="file"> picker (#98). Android
    // WebViews drop file-input taps unless we override onShowFileChooser, launch
    // a picker, and hand the chosen Uri(s) back through this callback.
    // Static (#210): the chrome client outlives any single activity.
    private static ValueCallback<Uri[]> filePathCallback;
    private static final int REQUEST_FILE_CHOOSER = 0xF11E;

    // When true, the web UI has explicitly asked for the system bars to be
    // visible (e.g. reader menu is open). onWindowFocusChanged respects this
    // so the bars don't immediately snap back to hidden on the next focus
    // event. Cleared again when the web UI calls hideSystemBars().
    // Static (#210): the preference must survive activity recreation.
    private static boolean systemBarsRequested = false;

    // Weak self-reference so PlaybackService (same process) can route media
    // button / notification actions back into the WebView's <audio> via JS,
    // and so the retained WebView's clients/bridge (#210) always act on the
    // CURRENT activity.
    private static WeakReference<MainActivity> sRef;

    static MainActivity cur() {
        return (sRef != null) ? sRef.get() : null;
    }

    // Called from PlaybackService's MediaSession callback. `action` is one of
    // play / pause / next / prev / forward / backward / seek:<ms>. Forwarded
    // to window.__mediaControl in player.js, which drives the <audio> element.
    // Targets the retained WebView directly (#210) so lock-screen / headphone
    // controls keep working even mid-recreation, when no activity is current.
    static void dispatchMedia(final String action) {
        final WebView wv = sWebView;
        if (wv == null || action == null) return;
        // action is a fixed vocabulary (ascii + digits); single-quote safe.
        wv.post(() -> wv.evaluateJavascript(
            "window.__mediaControl && window.__mediaControl('" + action + "')", null));
    }

    private static void startMediaService(android.content.Context c, Intent i) {
        try {
            if (PlaybackService.ACTION_STOP.equals(i.getAction())) {
                c.startService(i);  // not foreground — service will stop itself
            } else if (android.os.Build.VERSION.SDK_INT >= 26) {
                c.startForegroundService(i);
            } else {
                c.startService(i);
            }
        } catch (Exception e) {
            android.util.Log.e("Ereader", "startMediaService failed", e);
        }
    }

    // Page-driven window state that must survive activity recreation (#210):
    // the reader asks for these once on boot; a fold would otherwise silently
    // drop them with the old window.
    private static boolean keepScreenOnWanted = false;
    private static float brightnessWanted = -1f;

    // #242: launcher-icon variants exposed as <activity-alias> in the manifest.
    // Exactly one is enabled at a time; the web UI swaps by calling
    // JsBridge.setLauncherIcon(), which enables the chosen alias and disables the
    // rest via PackageManager — no rebuild. Names must match the manifest aliases
    // (Icon + CamelCase(variant)); the resource/variant form uses underscores.
    private static final String[] ICON_VARIANTS =
        {"default", "red", "blue", "purple", "pride", "lesbian", "bi_pride", "white"};

    private static String aliasFor(String v) {
        StringBuilder sb = new StringBuilder("Icon");
        for (String part : v.split("_")) {
            if (part.isEmpty()) continue;
            sb.append(Character.toUpperCase(part.charAt(0))).append(part.substring(1));
        }
        return sb.toString();
    }

    static void applyLauncherIcon(android.content.Context ctx, String variant) {
        if (ctx == null || variant == null) return;
        final String v0 = variant.replace('-', '_').toLowerCase();
        boolean known = false;
        for (String v : ICON_VARIANTS) if (v.equals(v0)) known = true;
        if (!known) return;
        PackageManager pm = ctx.getPackageManager();
        String pkg = ctx.getPackageName();
        // Enable the target FIRST so a launcher component always exists, then disable the rest.
        try {
            pm.setComponentEnabledSetting(new android.content.ComponentName(pkg, pkg + "." + aliasFor(v0)),
                PackageManager.COMPONENT_ENABLED_STATE_ENABLED, PackageManager.DONT_KILL_APP);
        } catch (Exception e) { android.util.Log.e("Ereader", "enable icon " + v0, e); }
        for (String v : ICON_VARIANTS) {
            if (v.equals(v0)) continue;
            try {
                pm.setComponentEnabledSetting(new android.content.ComponentName(pkg, pkg + "." + aliasFor(v)),
                    PackageManager.COMPONENT_ENABLED_STATE_DISABLED, PackageManager.DONT_KILL_APP);
            } catch (Exception e) { android.util.Log.e("Ereader", "disable icon " + v, e); }
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // #207: a fold-posture/display switch could spawn a SECOND MainActivity
        // while the first stayed alive — its WebView kept playing audio (the
        // foreground PlaybackService keeps the process hot) while this new one
        // rehydrated into a paused player at a stale position. launchMode=
        // singleTask (manifest) prevents the second instance; this guard
        // finishes any older survivor (and with it, its WebView's audio) if one
        // slips through anyway.
        MainActivity prev = (sRef != null) ? sRef.get() : null;
        if (prev != null && prev != this && !prev.isFinishing()) {
            prev.finish();
        }

        sRef = new WeakReference<>(this);

        // Android 13+ gates notifications (incl. the media-control notification
        // that backs lock-screen / headphone controls) behind a runtime grant.
        if (android.os.Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                new String[]{ android.Manifest.permission.POST_NOTIFICATIONS }, 1);
        }

        // System bars: hide the STATUS bar (clock / battery / wifi) entirely,
        // but keep the NAVIGATION bar (gesture pill) visible so Android's
        // swipe-up-home / edge-swipe-back gestures work without first summoning
        // it. applyImmersive() (also re-run on focus / config changes) is the
        // single place that drives this.
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_FORCE_NOT_FULLSCREEN);

        // Edge-to-edge. On API 30+ we use setDecorFitsSystemWindows(false)
        // instead of FLAG_LAYOUT_NO_LIMITS: NO_LIMITS pins the status bar into a
        // transparent-but-PRESENT state that the InsetsController can't hide
        // (the clock/battery icons stay drawn). setDecorFitsSystemWindows gives
        // the same edge-to-edge layout while still letting us hide it outright.
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(false);
        } else {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);
        }

        // Make both bars chrome-less: transparent, no contrast scrim, no divider
        // — so the visible nav bar shows only the gesture pill, and the status
        // bar (on the rare transient swipe-reveal) carries no background.
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.P) {
            getWindow().setNavigationBarDividerColor(Color.TRANSPARENT);
        }
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
            getWindow().setNavigationBarContrastEnforced(false);
        }

        // Resize the content for the soft keyboard so the in-book search box
        // stays above it (releaseImmersive() re-fits the decor to make this work
        // under edge-to-edge).
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);

        // Cutout mode
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.P) {
            WindowManager.LayoutParams layoutParams = getWindow().getAttributes();
            layoutParams.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            getWindow().setAttributes(layoutParams);
        }

        // #210/#240: re-apply page-driven window state that lived on the previous
        // activity's window (recreation gets a fresh window with default flags) —
        // keep-awake + the physical-session screen brightness — BEFORE the WebView
        // attaches, so a fold-triggered recreate doesn't flash to system/auto
        // brightness. Also re-asserted in onResume + onConfigurationChanged.
        applyWindowPowerState();

        // #210: adopt the retained WebView if one exists (fold/recreation) —
        // NO reload, the live page carries over. Only a truly fresh process
        // builds the WebView and loads "/" (where #198 rehydration applies).
        if (sWebView == null) {
            sWebCtx = new android.content.MutableContextWrapper(this);
            webView = createRetainedWebView(sWebCtx);
            sWebView = webView;
            setContentView(webView);
            // #275: decide the entry load HERE rather than optimistically hitting the
            // remote host and hoping the fallbacks catch it. Fully offline, the old
            // path ended with an empty document — and since the WebView paints
            // Color.BLACK behind it, that is the black screen. Offline we go straight
            // to the bundled offline Home instead of touching the network at all.
            boolean netUp = isOnline(this);
            NativeDiag.note(getApplicationContext(), "cold_launch", NativeDiag.d("online", netUp));
            if (netUp) {
                webView.loadUrl("http://100.69.184.113:8090/");
            } else {
                if (sClient != null) sClient.forceOffline();
                webView.loadUrl(OFFLINE_HOME_URL + "?why=cold");
            }
        } else {
            sWebCtx.setBaseContext(this);
            webView = sWebView;
            android.view.ViewGroup parent = (android.view.ViewGroup) webView.getParent();
            if (parent != null) parent.removeView(webView);
            setContentView(webView);
        }
    }

    // Build + fully wire the app's single retained WebView (#210). Static so
    // nothing here captures the creating activity: clients and the JS bridge
    // reach the CURRENT activity via cur()/sRef, or use the app context.
    private static WebView createRetainedWebView(android.content.MutableContextWrapper ctx) {
        final android.content.Context app = ctx.getApplicationContext();

        // Anonymous WebView subclass that suppresses the text-selection
        // floating toolbar (Copy / Share / Select All / Read Aloud / Web
        // Search) WITHOUT breaking selection itself.
        //
        // History of attempts:
        //   1. Returning null from startActionMode: kills the toolbar but
        //      ALSO breaks long-press selection on Chromium WebView — the
        //      selection engine depends on the ActionMode lifecycle to
        //      hand off touch state, so selecting becomes glitchy /
        //      impossible.
        //   2. mode.hide(Long.MAX_VALUE) alone: toolbar flickers back in
        //      because Chromium re-invalidates the action mode on every
        //      selectionchange.
        //
        // Working approach: wrap the caller's callback so:
        //   - onCreateActionMode / onPrepareActionMode return true but
        //     strip every menu item, so even if the toolbar surfaces it
        //     has nothing to show.
        //   - We also call mode.hide(Long.MAX_VALUE) on create/prepare to
        //     keep it invisible.
        //   - We forward onDestroyActionMode so Chromium's bookkeeping
        //     stays consistent and selection remains live.
        WebView wv = new WebView(ctx) {
            @Override
            public ActionMode startActionMode(ActionMode.Callback callback) {
                return super.startActionMode(wrapEmptyMenu(callback));
            }
            @Override
            public ActionMode startActionMode(ActionMode.Callback callback, int type) {
                return super.startActionMode(wrapEmptyMenu(callback), type);
            }
        };
        wv.setBackgroundColor(Color.BLACK);
        wv.setFitsSystemWindows(false);
        wv.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY);

        WebSettings webSettings = wv.getSettings();
        webSettings.setJavaScriptEnabled(true);
        webSettings.setDomStorageEnabled(true);
        webSettings.setAllowFileAccess(true);
        webSettings.setAllowContentAccess(true);
        webSettings.setBuiltInZoomControls(false);
        webSettings.setDisplayZoomControls(false);
        webSettings.setSupportZoom(false);
        webSettings.setLoadWithOverviewMode(true);
        webSettings.setUseWideViewPort(true);
        // Auto-update: never serve stale HTML/JS/CSS from the WebView's disk
        // cache. Combined with the server's Cache-Control: no-store, this makes
        // edits to web/*.html show up the next time the app is opened.
        webSettings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        // Allow <audio>/<video> to autoplay without a user gesture so ambient
        // reading music starts the moment an ebook opens. (#32)
        webSettings.setMediaPlaybackRequiresUserGesture(false);
        wv.clearCache(true);

        sClient = new OfflineShellWebViewClient(app);
        wv.setWebViewClient(sClient);
        // Custom chrome client so <input type="file"> (e.g. book-cover Upload on
        // the Books page) actually opens the system photo picker. (#98)
        wv.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view,
                                             ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                MainActivity a = cur();
                if (a == null) return false;
                // Drop any previous pending callback (cancel it) before storing ours.
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                try {
                    // createIntent() honors accept="image/*" + multiple from the
                    // page; wrap in a chooser so the user can pick gallery/files.
                    Intent intent = params.createIntent();
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    a.startActivityForResult(Intent.createChooser(intent, "Select image"),
                                             REQUEST_FILE_CHOOSER);
                    return true;
                } catch (Exception e) {
                    filePathCallback = null;
                    return false;   // let the WebView know we couldn't open a picker
                }
            }
        });

        // JS bridge: lets reader.html show/hide the Android system bars so
        // the user can use system gestures (swipe up to go home, etc) when
        // the in-app reader menu is open. Exposed as `window.Android`.
        wv.addJavascriptInterface(new JsBridge(app), "Android");

        // Hand off any non-HTML download (e.g. the APK self-update URL) to the
        // system DownloadManager instead of silently dropping it. Guard
        // against non-http(s) schemes (blob:, data:, file:) — DownloadManager
        // throws IllegalArgumentException on those, which would crash the
        // WebView process.
        wv.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent,
                                        String contentDisposition,
                                        String mimetype, long contentLength) {
                if (url == null) return;
                if (!url.startsWith("http://") && !url.startsWith("https://")) return;
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                String filename = URLUtil.guessFileName(url, contentDisposition, mimetype);
                req.setMimeType(mimetype);
                req.setNotificationVisibility(
                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(
                    Environment.DIRECTORY_DOWNLOADS, filename);
                DownloadManager dm = (DownloadManager) app.getSystemService(DOWNLOAD_SERVICE);
                dm.enqueue(req);
            }
        });

        return wv;
    }

    // Return the picked image Uri(s) to the WebView's file input. Always pass a
    // value back (null on cancel) or the input stays stuck and won't reopen. (#98)
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQUEST_FILE_CHOOSER) {
            if (filePathCallback != null) {
                Uri[] results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
                filePathCallback.onReceiveValue(results);
                filePathCallback = null;
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    // ---- Offline app shell (#23) ----
    // When the device has no network, serve the bundled copy of the web shell
    // (HTML/JS/CSS + vendored pdf.js/jszip/hls.js) from APK assets/web so the
    // app still launches and cached books still open. When ONLINE we return
    // null → the WebView loads live from the server exactly as before, so web
    // edits keep showing up without an APK rebuild. API / cover / GreatReads
    // requests are never bundled: offline they fail and the page's own
    // IndexedDB fallbacks take over. The bundled shell is staged into
    // assets/web by build-app.sh from web/.
    private static final String SHELL_HOST = "100.69.184.113";
    private static final String HOME_URL = "http://" + SHELL_HOST + ":8090/greatreads/";
    // #275: the offline landing. A bundled page that rebuilds GreatReads Home from
    // what an online visit cached (IndexedDB) — NOT the legacy ereader grid, which
    // is no longer on the offline path at all. Same origin as the online app, so
    // the cached books/covers/audiobooks are visible to it.
    private static final String OFFLINE_HOME_PATH = "/offline-home.html";
    private static final String OFFLINE_HOME_URL = "http://" + SHELL_HOST + ":8090" + OFFLINE_HOME_PATH;
    // The client owning the retained WebView, so resume/watchdog paths can force
    // the bundled shell for the next load instead of waiting out a TCP timeout.
    private static OfflineShellWebViewClient sClient;

    // #211: the WebView's render process died (fold/OOM). The retained view is
    // now a frozen brick — it ignores taps, JS, even loadUrl — and the OS
    // default for an unhandled renderer death is to kill the whole app (which
    // then relaunched into the same trap). Drop the corpse, build a fresh
    // retained WebView, and land on Home.
    static void recoverFromRenderGone(WebView dead) {
        try {
            android.view.ViewParent p = dead.getParent();
            if (p instanceof android.view.ViewGroup) ((android.view.ViewGroup) p).removeView(dead);
        } catch (Exception ignored) {}
        try { dead.destroy(); } catch (Exception ignored) {}
        if (sWebView == dead) sWebView = null;
        MainActivity a = cur();
        if (a == null || a.isFinishing()) return;   // next onCreate builds fresh → "/" → Home
        if (sWebCtx == null) sWebCtx = new android.content.MutableContextWrapper(a);
        sWebCtx.setBaseContext(a);
        WebView wv = createRetainedWebView(sWebCtx);
        sWebView = wv;
        a.webView = wv;
        a.setContentView(wv);
        wv.loadUrl(HOME_URL);
    }

    // Static (#210): the client lives on the retained WebView, so it must not
    // capture any activity — assets/connectivity come from the app context, and
    // the forced-offline latch lives here (it was an activity field).
    private static class OfflineShellWebViewClient extends WebViewClient {
        private final android.content.Context app;
        // Set when the main page failed to load from the network (offline, or
        // the host is unreachable even though ConnectivityManager reports
        // "online"). While true, shouldInterceptRequest serves the bundled
        // shell even if isOnline() is true — so we recover regardless of the
        // connectivity read. Reset on the next successful page load. (#23)
        private boolean forcedOfflineReload = false;

        OfflineShellWebViewClient(android.content.Context app) { this.app = app; }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            try {
                if (request == null || !"GET".equalsIgnoreCase(request.getMethod())) return null;
                Uri url = request.getUrl();
                if (url == null || !SHELL_HOST.equals(url.getHost())) return null;
                // Online → load live (preserves live-reload of the web shell) —
                // unless a prior main-frame load already failed, in which case
                // we force the bundled shell regardless of the connectivity read.
                if (isOnline(app) && !forcedOfflineReload) return null;
                // Offline → try to serve the request from the bundled shell.
                // NOTE: the root URL ("http://host:8090") has an EMPTY path, not
                // "/", so we must treat null/""/"/" all as the offline home.
                String path = url.getPath();
                if (path == null) path = "";
                // Any main-frame navigation to the server-rendered app — the root
                // bootstrap or /greatreads/… — becomes the offline Home (#275).
                // Sub-resources are left alone so they still resolve from the
                // bundle by their own path (or fail and let the page cope).
                boolean rootish = path.isEmpty() || path.equals("/") || path.equals("/index.html");
                boolean mainFrame = request.isForMainFrame();
                if (mainFrame && (rootish || path.startsWith("/greatreads"))) {
                    path = OFFLINE_HOME_PATH;
                } else if (rootish) {
                    path = OFFLINE_HOME_PATH;
                }
                if (mainFrame) {
                    NativeDiag.note(app, "intercept", NativeDiag.d(
                        "requested", url.getPath(), "serving", path,
                        "online", isOnline(app), "forced", forcedOfflineReload));
                }
                String assetPath = "web" + path;   // e.g. web/reader.html, web/vendor/pdf.min.js
                try {
                    InputStream is = app.getAssets().open(assetPath);
                    String mime = mimeFor(path);
                    android.util.Log.i("EreaderOffline", "served from bundle: " + path);
                    return new WebResourceResponse(mime, isTextMime(mime) ? "utf-8" : null, is);
                } catch (IOException notBundled) {
                    // Not part of the shell (API, covers, /greatreads/, …) → let
                    // it hit the network and fail; the page handles offline.
                    android.util.Log.i("EreaderOffline", "not bundled (network): " + path);
                    if (mainFrame) {
                        NativeDiag.note(app, "intercept_passthru", NativeDiag.d("path", path));
                    }
                    return null;
                }
            } catch (Exception e) {
                return null;
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            // Main page couldn't load from the network (offline, or the host is
            // unreachable while the device still reports connectivity). Fall back
            // to the bundled shell by reloading the SAME http URL — shouldInter-
            // ceptRequest then serves it from assets, keeping the origin (so the
            // IndexedDB cache stays visible). Guard against a reload loop.
            if (request != null && request.isForMainFrame()) {
                NativeDiag.note(app, "load_error", NativeDiag.d(
                    "url", String.valueOf(request.getUrl()),
                    "code", (error != null ? error.getErrorCode() : 0),
                    "desc", (error != null ? String.valueOf(error.getDescription()) : ""),
                    "already_forced", forcedOfflineReload));
            }
            if (request != null && request.isForMainFrame() && !forcedOfflineReload) {
                forcedOfflineReload = true;
                // Retry the URL that actually FAILED, not the root — reloading
                // root dumped the user on Home mid-book whenever a transient
                // blip (e.g. Tailscale reconnecting right after unlock) broke a
                // WebView-initiated reload of reader.html/player.html (#198).
                // Only retry same-URL when the bundled shell can serve that
                // path; otherwise (API pages, /greatreads/…) fall back to root
                // as before so the user at least gets the offline home.
                String target = OFFLINE_HOME_URL + "?why=error";
                Uri failed = request.getUrl();
                if (failed != null && SHELL_HOST.equals(failed.getHost())) {
                    String path = failed.getPath();
                    if (path != null && !path.isEmpty() && !path.equals("/")) {
                        try {
                            app.getAssets().open("web" + path).close();
                            target = failed.toString();
                        } catch (IOException notBundled) { /* root fallback */ }
                    }
                }
                android.util.Log.i("EreaderOffline",
                    "main-frame load failed → bundled shell fallback: " + target);
                final String t = target;
                view.post(() -> view.loadUrl(t));
                return;
            }
            super.onReceivedError(view, request, error);
        }

        // Let resume/watchdog paths pin the next load to the bundled shell instead
        // of making the user wait out a TCP timeout to the missing host (#275).
        void forceOffline() { forcedOfflineReload = true; }

        // The main-frame URL currently loading, for the stall watchdog below.
        private String pendingMainUrl = null;

        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
            super.onPageStarted(view, url, favicon);
            if (url == null || !url.contains(SHELL_HOST)) return;
            if (isBundledPage(url)) return;   // local once intercepted; cannot stall
            // A server-rendered page against an unreachable host does not fail fast —
            // it hangs on the connect timeout, which is what produced the black
            // screen (#270). If it hasn't painted in 6s, ask the network directly
            // and land on the offline Home rather than waiting it out (#275).
            final String started = url;
            pendingMainUrl = url;
            view.postDelayed(() -> {
                if (!started.equals(pendingMainUrl)) return;      // finished or superseded
                new Thread(() -> {
                    final boolean up = hostReachable();
                    view.post(() -> {
                        if (!started.equals(pendingMainUrl) || up) return;
                        android.util.Log.i("EreaderOffline",
                            "main-frame load stalled + host unreachable → offline home");
                        NativeDiag.note(app, "stalled", NativeDiag.d("url", started));
                        forcedOfflineReload = true;
                        pendingMainUrl = null;
                        view.loadUrl(OFFLINE_HOME_URL + "?why=stalled");
                    });
                }).start();
            }, 6000);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            // A page loaded successfully — clear the forced-offline latch so the
            // next navigation tries the network (live) again.
            forcedOfflineReload = false;
            pendingMainUrl = null;
            super.onPageFinished(view, url);

            // Whatever the device recorded while it was cut off goes out now.
            if (isOnline(app)) new Thread(() -> NativeDiag.flush(app)).start();

            // Last-resort net for the black screen (#275): a "finished" load that
            // produced an EMPTY document renders as the WebView's black background
            // with no error and no page to report it. If that happens on a
            // server-rendered URL, land on the offline Home instead of a void.
            if (url == null || isBundledPage(url) || !url.contains(SHELL_HOST)) return;
            view.evaluateJavascript(
                "(function(){try{return document.body?document.body.childElementCount:0;}catch(e){return -1;}})()",
                v -> {
                    int kids;
                    try { kids = Integer.parseInt(String.valueOf(v).trim()); } catch (Exception e) { kids = -1; }
                    if (kids > 0) return;
                    NativeDiag.note(app, "blank_page", NativeDiag.d("url", url, "children", kids));
                    android.util.Log.i("EreaderOffline", "blank document after load → offline home: " + url);
                    forcedOfflineReload = true;
                    view.loadUrl(OFFLINE_HOME_URL + "?why=blank");
                });
        }

        @Override
        public boolean onRenderProcessGone(WebView view, android.webkit.RenderProcessGoneDetail detail) {
            android.util.Log.e("Ereader", "WebView renderer gone (didCrash="
                + (detail != null && detail.didCrash()) + ") — rebuilding on Home (#211)");
            MainActivity.recoverFromRenderGone(view);
            return true;   // handled — do NOT let the OS kill the whole app
        }
    }

    // ---- Native launch diagnostics (#275) -----------------------------------
    // gr-diag.js can only report from a page that rendered — useless for the exact
    // failure we are chasing, where nothing renders and the WebView's black
    // background is all you see. So the shell keeps its own ring buffer on disk and
    // ships it to the server the next time a page loads online. This is the device
    // visibility the #270 post-mortem said we had to get before anything else.
    static class NativeDiag {
        private static final String PREFS = "ereader_diag";
        private static final String KEY = "queue";
        private static final int MAX = 40;

        static synchronized void note(android.content.Context app, String event, org.json.JSONObject detail) {
            try {
                android.content.SharedPreferences p =
                    app.getSharedPreferences(PREFS, android.content.Context.MODE_PRIVATE);
                org.json.JSONArray q = new org.json.JSONArray(p.getString(KEY, "[]"));
                org.json.JSONObject o = new org.json.JSONObject();
                o.put("event", event);
                o.put("at", new java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'",
                        java.util.Locale.US).format(new java.util.Date()));
                org.json.JSONObject d = (detail != null) ? detail : new org.json.JSONObject();
                d.put("src", "native");
                o.put("detail", d);
                q.put(o);
                while (q.length() > MAX) q.remove(0);   // ring buffer: a long trip must not grow forever
                p.edit().putString(KEY, q.toString()).apply();
                android.util.Log.i("EreaderOffline", "diag " + event + " " + d);
            } catch (Exception ignored) {}
        }

        static org.json.JSONObject d(Object... kv) {
            org.json.JSONObject o = new org.json.JSONObject();
            try {
                for (int i = 0; i + 1 < kv.length; i += 2) o.put(String.valueOf(kv[i]), kv[i + 1]);
            } catch (Exception ignored) {}
            return o;
        }

        // Ship everything queued, then clear. Background thread only.
        static void flush(android.content.Context app) {
            String payload;
            synchronized (NativeDiag.class) {
                String q = app.getSharedPreferences(PREFS, android.content.Context.MODE_PRIVATE)
                              .getString(KEY, "[]");
                if (q == null || "[]".equals(q) || q.isEmpty()) return;
                payload = "{\"events\":" + q + "}";
            }
            java.net.HttpURLConnection c = null;
            try {
                c = (java.net.HttpURLConnection) new java.net.URL(
                        "http://" + SHELL_HOST + ":8092/api/client-events").openConnection();
                c.setRequestMethod("POST");
                c.setRequestProperty("Content-Type", "application/json");
                c.setDoOutput(true);
                c.setConnectTimeout(3000);
                c.setReadTimeout(3000);
                c.getOutputStream().write(payload.getBytes("UTF-8"));
                int code = c.getResponseCode();
                if (code >= 200 && code < 300) {
                    synchronized (NativeDiag.class) {
                        app.getSharedPreferences(PREFS, android.content.Context.MODE_PRIVATE)
                           .edit().putString(KEY, "[]").apply();
                    }
                }
            } catch (Exception ignored) {
            } finally {
                if (c != null) try { c.disconnect(); } catch (Exception ignored) {}
            }
        }
    }

    // A bundled page keeps working with no host — never redirect away from one.
    private static boolean isBundledPage(String url) {
        return url.contains("offline-home.html") || url.contains("reader.html") || url.contains("player.html");
    }

    // Is the actual host reachable? ConnectivityManager only knows the radio is up,
    // which is exactly wrong for the camping case (WiFi present, Tailscale host
    // gone) and for a laggy reconnect. Two quick tries so a single dropped packet
    // doesn't bounce the user out of a working page. Never call on the main thread.
    private static boolean hostReachable() {
        for (int i = 0; i < 2; i++) {
            java.net.HttpURLConnection c = null;
            try {
                c = (java.net.HttpURLConnection) new java.net.URL(HOME_URL).openConnection();
                c.setRequestMethod("HEAD");
                c.setConnectTimeout(2000);
                c.setReadTimeout(2000);
                if (c.getResponseCode() > 0) return true;
            } catch (Exception ignored) {
            } finally {
                if (c != null) try { c.disconnect(); } catch (Exception ignored) {}
            }
        }
        return false;
    }

    private static boolean isOnline(android.content.Context ctx) {
        try {
            ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(CONNECTIVITY_SERVICE);
            if (cm == null) return true;            // can't tell → assume online (load live)
            Network n = cm.getActiveNetwork();
            if (n == null) return false;
            NetworkCapabilities caps = cm.getNetworkCapabilities(n);
            return caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        } catch (Exception e) {
            return true;
        }
    }

    private static boolean isTextMime(String m) {
        return m != null && (m.startsWith("text/") || m.equals("application/javascript")
                || m.equals("application/json") || m.equals("image/svg+xml"));
    }

    private static String mimeFor(String path) {
        String p = path.toLowerCase();
        if (p.endsWith(".html") || p.endsWith(".htm")) return "text/html";
        if (p.endsWith(".js"))    return "application/javascript";
        if (p.endsWith(".css"))   return "text/css";
        if (p.endsWith(".json"))  return "application/json";
        if (p.endsWith(".svg"))   return "image/svg+xml";
        if (p.endsWith(".png"))   return "image/png";
        if (p.endsWith(".jpg") || p.endsWith(".jpeg")) return "image/jpeg";
        if (p.endsWith(".gif"))   return "image/gif";
        if (p.endsWith(".webp"))  return "image/webp";
        if (p.endsWith(".ico"))   return "image/x-icon";
        if (p.endsWith(".woff2")) return "font/woff2";
        if (p.endsWith(".woff"))  return "font/woff";
        if (p.endsWith(".ttf"))   return "font/ttf";
        if (p.endsWith(".mp3"))   return "audio/mpeg";
        return "application/octet-stream";
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        // Don't slam the bars back to hidden if the web UI just asked for
        // them — that would defeat the whole point of showSystemBars().
        if (hasFocus && !systemBarsRequested) {
            applyImmersive();
        }
    }

    private void applyImmersive() {
        // Hide the STATUS bar only; keep the navigation bar (gesture pill)
        // visible at all times so Android system gestures (swipe-up home,
        // edge-swipe back) are always available without summoning the pill.
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            // Controller ONLY on API 30+. Mixing the deprecated
            // setSystemUiVisibility() flags here resets the controller's state
            // and lets the status bar slip back in (transparent but present).
            // Restore edge-to-edge in case releaseImmersive() turned it off.
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                // Status bar can still be pulled down transiently by a swipe.
                c.setSystemBarsBehavior(
                    WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                c.hide(WindowInsets.Type.statusBars());
                c.show(WindowInsets.Type.navigationBars());
            }
        } else {
            // Legacy (< API 30): FLAG_FULLSCREEN + SYSTEM_UI_FLAG_FULLSCREEN
            // hide the status bar; we deliberately leave navigation visible.
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
            getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_FULLSCREEN
            );
        }
    }

    private void releaseImmersive() {
        // Called while the web UI needs the soft keyboard (in-book search). We
        // let the decor fit system windows again so the IME resizes the content
        // (the search box stays above the keyboard), but we KEEP the status bar
        // hidden — the user never wants the clock/battery back — and keep the
        // nav pill visible. hideSystemBars()/applyImmersive() restores
        // edge-to-edge afterwards.
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(true);
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.setSystemBarsBehavior(
                    WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                c.hide(WindowInsets.Type.statusBars());
                c.show(WindowInsets.Type.navigationBars());
            }
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
            getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            );
        }
    }

    // Wrap a WebView-supplied ActionMode.Callback so that the floating
    // text-selection toolbar never actually appears, while the underlying
    // ActionMode is still created and destroyed normally so Chromium's
    // selection state machine stays healthy.
    private static ActionMode.Callback wrapEmptyMenu(final ActionMode.Callback inner) {
        return new ActionMode.Callback() {
            @Override
            public boolean onCreateActionMode(ActionMode mode, Menu menu) {
                // Let Chromium populate, then clear and hide immediately.
                boolean r = inner != null && inner.onCreateActionMode(mode, menu);
                if (menu != null) menu.clear();
                try { mode.hide(Long.MAX_VALUE); } catch (Throwable ignored) {}
                return r;
            }
            @Override
            public boolean onPrepareActionMode(ActionMode mode, Menu menu) {
                if (inner != null) inner.onPrepareActionMode(mode, menu);
                if (menu != null) menu.clear();
                try { mode.hide(Long.MAX_VALUE); } catch (Throwable ignored) {}
                return true;
            }
            @Override
            public boolean onActionItemClicked(ActionMode mode, MenuItem item) {
                return false;
            }
            @Override
            public void onDestroyActionMode(ActionMode mode) {
                if (inner != null) inner.onDestroyActionMode(mode);
            }
        };
    }

    /** JS bridge surface exposed to the WebView as `window.Android`.
     * Static (#210): it lives on the retained WebView and outlives any single
     * activity — window-bound operations (bars, wake lock, brightness) target
     * the CURRENT activity via cur(); service intents + file sharing use the
     * application context. */
    private static class JsBridge {
        private final android.content.Context app;
        private final android.os.Handler main =
            new android.os.Handler(android.os.Looper.getMainLooper());
        JsBridge(android.content.Context app) { this.app = app; }

        @JavascriptInterface
        public void showSystemBars() {
            systemBarsRequested = true;
            main.post(() -> { MainActivity a = cur(); if (a != null) a.releaseImmersive(); });
        }
        @JavascriptInterface
        public void hideSystemBars() {
            systemBarsRequested = false;
            main.post(() -> { MainActivity a = cur(); if (a != null) a.applyImmersive(); });
        }
        // Keep the screen on while reading. Honours the "Keep screen awake"
        // toggle in Settings. Sets/clears FLAG_KEEP_SCREEN_ON on the
        // activity window, which is the canonical way to inhibit the OS
        // display timeout (the Web Wake Lock API silently no-ops in many
        // Android WebView configurations, so this is the reliable path).
        // The wanted state is remembered so a recreated activity (#210 fold)
        // re-applies it to its fresh window.
        @JavascriptInterface
        public void keepScreenOn(final boolean on) {
            keepScreenOnWanted = on;
            main.post(() -> {
                MainActivity a = cur(); if (a == null) return;
                if (on) {
                    a.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                } else {
                    a.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                }
            });
        }
        // Screen-as-light "reading lamp" (#40): set the activity window brightness
        // (0.0–1.0). Pass -1 (BRIGHTNESS_OVERRIDE_NONE) to restore the system
        // default. Window-level brightness needs no WRITE_SETTINGS permission and
        // only applies while this app is foreground — exactly like a lamp app.
        @JavascriptInterface
        public void setBrightness(final float level) {
            brightnessWanted = level;
            main.post(() -> {
                MainActivity a = cur(); if (a == null) return;
                WindowManager.LayoutParams lp = a.getWindow().getAttributes();
                lp.screenBrightness = level;
                a.getWindow().setAttributes(lp);
            });
        }
        // ---- Background audiobook playback + media controls ----
        // The player (player.js) calls these to drive the foreground
        // PlaybackService that keeps audio alive when the screen locks and
        // owns the MediaSession hardware/headphone buttons talk to.
        // mediaStart: begin/refresh the session with this book's metadata.
        @JavascriptInterface
        public void mediaStart(String title, String artist, String coverUrl) {
            Intent i = new Intent(app, PlaybackService.class)
                    .setAction(PlaybackService.ACTION_START);
            i.putExtra("title", title);
            i.putExtra("artist", artist);
            i.putExtra("coverUrl", coverUrl);
            i.putExtra("playing", true);
            startMediaService(app, i);
        }
        // mediaState: push the current play/pause state + book-global position
        // (seconds), total duration (seconds) and playback rate. While the
        // service is already running we update it in-process (no background FGS
        // start, which Android 12+ blocks once the screen is locked).
        @JavascriptInterface
        public void mediaState(final boolean playing, final double position,
                               final double duration, final double rate) {
            main.post(() -> {
                if (PlaybackService.isRunning()) {
                    PlaybackService.applyState(playing, position, duration, rate);
                    return;
                }
                Intent i = new Intent(app, PlaybackService.class)
                        .setAction(PlaybackService.ACTION_UPDATE);
                i.putExtra("playing", playing);
                i.putExtra("position", position);
                i.putExtra("duration", duration);
                i.putExtra("rate", rate);
                startMediaService(app, i);
            });
        }
        // mediaStop: tear down the session + notification (player closed).
        @JavascriptInterface
        public void mediaStop() {
            main.post(() -> {
                if (PlaybackService.isRunning()) { PlaybackService.stopFromBridge(); return; }
                Intent i = new Intent(app, PlaybackService.class)
                        .setAction(PlaybackService.ACTION_STOP);
                startMediaService(app, i);
            });
        }
        // Share a PNG image generated client-side (canvas → base64). The
        // Web Share API requires a secure context, but our WebView loads
        // over plain HTTP from Tailscale, so we cannot rely on
        // navigator.share({files}). Instead the JS encodes the canvas to
        // base64 and hands it here; we drop it in cacheDir/share, wrap a
        // FileProvider content:// URI around it, and fire ACTION_SEND. The
        // system chooser includes "Save to Photos", every messaging app,
        // Drive, etc.
        @JavascriptInterface
        public void shareImage(final String base64Png, final String chooserTitle) {
            main.post(() -> {
                try {
                    byte[] bytes = android.util.Base64.decode(base64Png, android.util.Base64.DEFAULT);
                    java.io.File dir = new java.io.File(app.getCacheDir(), "share");
                    if (!dir.exists()) dir.mkdirs();
                    java.io.File outFile = new java.io.File(dir,
                        "greatreads-quote-" + System.currentTimeMillis() + ".png");
                    java.io.FileOutputStream fos = new java.io.FileOutputStream(outFile);
                    try { fos.write(bytes); } finally { fos.close(); }
                    android.net.Uri uri = androidx.core.content.FileProvider.getUriForFile(
                        app, app.getPackageName() + ".fileprovider", outFile);
                    Intent send = new Intent(Intent.ACTION_SEND);
                    send.setType("image/png");
                    send.putExtra(Intent.EXTRA_STREAM, uri);
                    send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    Intent chooser = Intent.createChooser(send,
                        chooserTitle != null && !chooserTitle.isEmpty() ? chooserTitle : "Share quote");
                    chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    app.startActivity(chooser);
                } catch (Exception e) {
                    android.util.Log.e("Ereader", "shareImage failed", e);
                }
            });
        }

        @JavascriptInterface
        public void setLauncherIcon(final String variant) {
            // #242: swap the launcher icon among the pre-baked variant aliases.
            // Uses the application context, so it works even mid-recreation.
            main.post(() -> applyLauncherIcon(app, variant));
        }
    }

    @Override
    public void onBackPressed() {
        // #208: on a book page (reader/player), back ALWAYS goes Home — never
        // exits the app, never walks WebView history. (History tricks don't
        // work anyway: Chromium skips history entries added without a user
        // gesture, so canGoBack() lies on a cold-rehydrated page, and exiting
        // just rehydrated straight back into the book — an exit trap.)
        // The page gets first crack via window.grHandleBack() so an OPEN
        // overlay (dictionary/wiki/ebook-on-player) closes instead; any other
        // result — false, error, no hook — lands on Home. NOT "/": the root
        // bootstrap would bounce right back into the book.
        String u = webView.getUrl();
        if (u != null && (u.contains("reader.html") || u.contains("player.html"))) {
            // Watchdog (#211): grHandleBack needs the page's JS to be ALIVE. A
            // wedged/dead page never answers — back must go Home regardless, so
            // if no verdict lands within 300ms we navigate anyway.
            final boolean[] settled = { false };
            final Runnable goHome = () -> {
                if (settled[0]) return;
                settled[0] = true;
                webView.loadUrl(HOME_URL);
            };
            webView.postDelayed(goHome, 300);
            webView.evaluateJavascript(
                "(function(){try{return !!(window.grHandleBack&&window.grHandleBack());}catch(e){return false;}})()",
                v -> {
                    if ("true".equals(v)) settled[0] = true;   // overlay closed — handled
                    else goHome.run();
                });
            return;
        }
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        // #240: re-assert brightness / keep-awake on every resume — covers fold
        // paths that stop→resume the activity without a full recreate, so the
        // physical-session screen brightness never drops back to system default.
        applyWindowPowerState();
        // #275: the retained WebView (#210) means resuming does NOT re-run the load,
        // so coming back after losing signal leaves a server page that is stale, blank,
        // or about to fail. Check the host and land on the offline Home if it's gone.
        maybeLandOfflineOnResume();
    }

    // Probe off the main thread; only redirect a server-rendered page, never a
    // bundled one the user is actively reading in. The offline Home returns to the
    // real Home by itself once the host answers again.
    private void maybeLandOfflineOnResume() {
        final WebView wv = webView;
        if (wv == null) return;
        final String u = wv.getUrl();
        if (u == null || !u.contains(SHELL_HOST) || isBundledPage(u)) return;
        new Thread(() -> {
            if (hostReachable()) return;
            wv.post(() -> {
                android.util.Log.i("EreaderOffline", "resume with host unreachable → offline home");
                NativeDiag.note(getApplicationContext(), "resume_offline", NativeDiag.d("url", u));
                if (sClient != null) sClient.forceOffline();
                wv.loadUrl(OFFLINE_HOME_URL + "?why=resume");
            });
        }).start();
    }

    @Override
    protected void onDestroy() {
        if (sRef != null && sRef.get() == this) sRef = null;
        super.onDestroy();
    }

    // #240: (re)apply the page-driven window POWER state — FLAG_KEEP_SCREEN_ON and
    // the physical-session screen brightness — to THIS activity's window. A fold
    // can tear down + recreate the activity (a physical display switch) with a
    // fresh window at default brightness; without a prompt re-apply the screen
    // visibly flashes to system/auto brightness and back. Idempotent; safe to call
    // from onCreate (pre-attach), onResume, and onConfigurationChanged.
    private void applyWindowPowerState() {
        if (keepScreenOnWanted) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
        if (brightnessWanted >= 0f) {
            WindowManager.LayoutParams blp = getWindow().getAttributes();
            blp.screenBrightness = brightnessWanted;
            getWindow().setAttributes(blp);
        }
    }

    // Foldable posture changes (fold <-> unfold) fire onConfigurationChanged
    // instead of recreating the Activity because the manifest declares
    // android:configChanges. We just need to re-apply the immersive UI flags
    // so the system bars don't pop back in after the new layout pass.
    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        applyWindowPowerState();   // #240: a posture change must not drop brightness/keep-awake
        if (systemBarsRequested) {
            releaseImmersive();
        } else {
            applyImmersive();
        }
    }
}
