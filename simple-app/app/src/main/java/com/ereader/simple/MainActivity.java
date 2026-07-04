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

    // Pending callback for a WebView <input type="file"> picker (#98). Android
    // WebViews drop file-input taps unless we override onShowFileChooser, launch
    // a picker, and hand the chosen Uri(s) back through this callback.
    private ValueCallback<Uri[]> filePathCallback;
    private static final int REQUEST_FILE_CHOOSER = 0xF11E;

    // When true, the web UI has explicitly asked for the system bars to be
    // visible (e.g. reader menu is open). onWindowFocusChanged respects this
    // so the bars don't immediately snap back to hidden on the next focus
    // event. Cleared again when the web UI calls hideSystemBars().
    private boolean systemBarsRequested = false;

    // Set when the main page failed to load from the network (offline, or the
    // host is unreachable even though ConnectivityManager reports "online").
    // While true, shouldInterceptRequest serves the bundled shell even if
    // isOnline() is true — so we recover regardless of the connectivity read.
    // Reset on the next successful page load. (#23)
    private boolean forcedOfflineReload = false;

    // Weak self-reference so PlaybackService (same process) can route media
    // button / notification actions back into the WebView's <audio> via JS.
    private static WeakReference<MainActivity> sRef;

    // Called from PlaybackService's MediaSession callback. `action` is one of
    // play / pause / next / prev / forward / backward / seek:<ms>. Forwarded
    // to window.__mediaControl in player.js, which drives the <audio> element.
    static void dispatchMedia(final String action) {
        MainActivity a = (sRef != null) ? sRef.get() : null;
        if (a != null) a.runMediaControl(action);
    }

    private void runMediaControl(final String action) {
        if (action == null) return;
        runOnUiThread(() -> {
            if (webView == null) return;
            // action is a fixed vocabulary (ascii + digits); single-quote safe.
            webView.evaluateJavascript(
                "window.__mediaControl && window.__mediaControl('" + action + "')", null);
        });
    }

    private void startMediaService(Intent i) {
        try {
            if (PlaybackService.ACTION_STOP.equals(i.getAction())) {
                startService(i);  // not foreground — service will stop itself
            } else if (android.os.Build.VERSION.SDK_INT >= 26) {
                startForegroundService(i);
            } else {
                startService(i);
            }
        } catch (Exception e) {
            android.util.Log.e("Ereader", "startMediaService failed", e);
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
        webView = new WebView(this) {
            @Override
            public ActionMode startActionMode(ActionMode.Callback callback) {
                return super.startActionMode(wrapEmptyMenu(callback));
            }
            @Override
            public ActionMode startActionMode(ActionMode.Callback callback, int type) {
                return super.startActionMode(wrapEmptyMenu(callback), type);
            }
        };
        webView.setBackgroundColor(Color.BLACK);
        webView.setFitsSystemWindows(false);
        webView.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY);

        setContentView(webView);

        WebSettings webSettings = webView.getSettings();
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
        webView.clearCache(true);

        webView.setWebViewClient(new OfflineShellWebViewClient());
        // Custom chrome client so <input type="file"> (e.g. book-cover Upload on
        // the Books page) actually opens the system photo picker. (#98)
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view,
                                             ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
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
                    startActivityForResult(Intent.createChooser(intent, "Select image"),
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
        webView.addJavascriptInterface(new JsBridge(), "Android");

        // Hand off any non-HTML download (e.g. the APK self-update URL) to the
        // system DownloadManager instead of silently dropping it. Guard
        // against non-http(s) schemes (blob:, data:, file:) — DownloadManager
        // throws IllegalArgumentException on those, which would crash the
        // WebView process.
        webView.setDownloadListener(new DownloadListener() {
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
                DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                dm.enqueue(req);
            }
        });

        webView.loadUrl("http://100.69.184.113:8090/");
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

    private class OfflineShellWebViewClient extends WebViewClient {
        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            try {
                if (request == null || !"GET".equalsIgnoreCase(request.getMethod())) return null;
                Uri url = request.getUrl();
                if (url == null || !SHELL_HOST.equals(url.getHost())) return null;
                // Online → load live (preserves live-reload of the web shell) —
                // unless a prior main-frame load already failed, in which case
                // we force the bundled shell regardless of the connectivity read.
                if (isOnline() && !forcedOfflineReload) return null;
                // Offline → try to serve the request from the bundled shell.
                // NOTE: the root URL ("http://host:8090") has an EMPTY path, not
                // "/", so we must treat null/""/"/" all as the offline home.
                String path = url.getPath();
                if (path == null || path.isEmpty() || path.equals("/") || path.equals("/index.html")) {
                    path = "/index.html";   // offline home = the cached ereader grid
                }
                String assetPath = "web" + path;   // e.g. web/reader.html, web/vendor/pdf.min.js
                try {
                    InputStream is = getAssets().open(assetPath);
                    String mime = mimeFor(path);
                    android.util.Log.i("EreaderOffline", "served from bundle: " + path);
                    return new WebResourceResponse(mime, isTextMime(mime) ? "utf-8" : null, is);
                } catch (IOException notBundled) {
                    // Not part of the shell (API, covers, /greatreads/, …) → let
                    // it hit the network and fail; the page handles offline.
                    android.util.Log.i("EreaderOffline", "not bundled (network): " + path);
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
            if (request != null && request.isForMainFrame() && !forcedOfflineReload) {
                forcedOfflineReload = true;
                // Retry the URL that actually FAILED, not the root — reloading
                // root dumped the user on Home mid-book whenever a transient
                // blip (e.g. Tailscale reconnecting right after unlock) broke a
                // WebView-initiated reload of reader.html/player.html (#198).
                // Only retry same-URL when the bundled shell can serve that
                // path; otherwise (API pages, /greatreads/…) fall back to root
                // as before so the user at least gets the offline home.
                String target = "http://" + SHELL_HOST + ":8090/";
                Uri failed = request.getUrl();
                if (failed != null && SHELL_HOST.equals(failed.getHost())) {
                    String path = failed.getPath();
                    if (path != null && !path.isEmpty() && !path.equals("/")) {
                        try {
                            getAssets().open("web" + path).close();
                            target = failed.toString();
                        } catch (IOException notBundled) { /* root fallback */ }
                    }
                }
                android.util.Log.i("EreaderOffline",
                    "main-frame load failed → bundled shell fallback: " + target);
                final String t = target;
                runOnUiThread(() -> view.loadUrl(t));
                return;
            }
            super.onReceivedError(view, request, error);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            // A page loaded successfully — clear the forced-offline latch so the
            // next navigation tries the network (live) again.
            forcedOfflineReload = false;
            super.onPageFinished(view, url);
        }
    }

    private boolean isOnline() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
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

    /** JS bridge surface exposed to the WebView as `window.Android`. */
    private class JsBridge {
        @JavascriptInterface
        public void showSystemBars() {
            systemBarsRequested = true;
            runOnUiThread(MainActivity.this::releaseImmersive);
        }
        @JavascriptInterface
        public void hideSystemBars() {
            systemBarsRequested = false;
            runOnUiThread(MainActivity.this::applyImmersive);
        }
        // Keep the screen on while reading. Honours the "Keep screen awake"
        // toggle in Settings. Sets/clears FLAG_KEEP_SCREEN_ON on the
        // activity window, which is the canonical way to inhibit the OS
        // display timeout (the Web Wake Lock API silently no-ops in many
        // Android WebView configurations, so this is the reliable path).
        @JavascriptInterface
        public void keepScreenOn(final boolean on) {
            runOnUiThread(() -> {
                if (on) {
                    getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                } else {
                    getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                }
            });
        }
        // Screen-as-light "reading lamp" (#40): set the activity window brightness
        // (0.0–1.0). Pass -1 (BRIGHTNESS_OVERRIDE_NONE) to restore the system
        // default. Window-level brightness needs no WRITE_SETTINGS permission and
        // only applies while this app is foreground — exactly like a lamp app.
        @JavascriptInterface
        public void setBrightness(final float level) {
            runOnUiThread(() -> {
                WindowManager.LayoutParams lp = getWindow().getAttributes();
                lp.screenBrightness = level;
                getWindow().setAttributes(lp);
            });
        }
        // ---- Background audiobook playback + media controls ----
        // The player (player.js) calls these to drive the foreground
        // PlaybackService that keeps audio alive when the screen locks and
        // owns the MediaSession hardware/headphone buttons talk to.
        // mediaStart: begin/refresh the session with this book's metadata.
        @JavascriptInterface
        public void mediaStart(String title, String artist, String coverUrl) {
            Intent i = new Intent(MainActivity.this, PlaybackService.class)
                    .setAction(PlaybackService.ACTION_START);
            i.putExtra("title", title);
            i.putExtra("artist", artist);
            i.putExtra("coverUrl", coverUrl);
            i.putExtra("playing", true);
            startMediaService(i);
        }
        // mediaState: push the current play/pause state + book-global position
        // (seconds), total duration (seconds) and playback rate. While the
        // service is already running we update it in-process (no background FGS
        // start, which Android 12+ blocks once the screen is locked).
        @JavascriptInterface
        public void mediaState(final boolean playing, final double position,
                               final double duration, final double rate) {
            runOnUiThread(() -> {
                if (PlaybackService.isRunning()) {
                    PlaybackService.applyState(playing, position, duration, rate);
                    return;
                }
                Intent i = new Intent(MainActivity.this, PlaybackService.class)
                        .setAction(PlaybackService.ACTION_UPDATE);
                i.putExtra("playing", playing);
                i.putExtra("position", position);
                i.putExtra("duration", duration);
                i.putExtra("rate", rate);
                startMediaService(i);
            });
        }
        // mediaStop: tear down the session + notification (player closed).
        @JavascriptInterface
        public void mediaStop() {
            runOnUiThread(() -> {
                if (PlaybackService.isRunning()) { PlaybackService.stopFromBridge(); return; }
                Intent i = new Intent(MainActivity.this, PlaybackService.class)
                        .setAction(PlaybackService.ACTION_STOP);
                startMediaService(i);
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
            runOnUiThread(() -> {
                try {
                    byte[] bytes = android.util.Base64.decode(base64Png, android.util.Base64.DEFAULT);
                    java.io.File dir = new java.io.File(getCacheDir(), "share");
                    if (!dir.exists()) dir.mkdirs();
                    java.io.File outFile = new java.io.File(dir,
                        "greatreads-quote-" + System.currentTimeMillis() + ".png");
                    java.io.FileOutputStream fos = new java.io.FileOutputStream(outFile);
                    try { fos.write(bytes); } finally { fos.close(); }
                    android.net.Uri uri = androidx.core.content.FileProvider.getUriForFile(
                        MainActivity.this, getPackageName() + ".fileprovider", outFile);
                    Intent send = new Intent(Intent.ACTION_SEND);
                    send.setType("image/png");
                    send.putExtra(Intent.EXTRA_STREAM, uri);
                    send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    Intent chooser = Intent.createChooser(send,
                        chooserTitle != null && !chooserTitle.isEmpty() ? chooserTitle : "Share quote");
                    chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(chooser);
                } catch (Exception e) {
                    android.util.Log.e("Ereader", "shareImage failed", e);
                }
            });
        }
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (sRef != null && sRef.get() == this) sRef = null;
        super.onDestroy();
    }

    // Foldable posture changes (fold <-> unfold) fire onConfigurationChanged
    // instead of recreating the Activity because the manifest declares
    // android:configChanges. We just need to re-apply the immersive UI flags
    // so the system bars don't pop back in after the new layout pass.
    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        if (systemBarsRequested) {
            releaseImmersive();
        } else {
            applyImmersive();
        }
    }
}
