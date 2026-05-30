package com.ereader.simple;

import android.app.Activity;
import android.content.res.Configuration;
import android.os.Bundle;
import android.webkit.WebView;
import android.webkit.WebSettings;
import android.webkit.WebViewClient;
import android.webkit.WebChromeClient;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.JavascriptInterface;
import android.content.Intent;
import android.net.Uri;
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

public class MainActivity extends Activity {
    private WebView webView;

    // When true, the web UI has explicitly asked for the system bars to be
    // visible (e.g. reader menu is open). onWindowFocusChanged respects this
    // so the bars don't immediately snap back to hidden on the next focus
    // event. Cleared again when the web UI calls hideSystemBars().
    private boolean systemBarsRequested = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // AGGRESSIVE fullscreen setup
        getWindow().getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
        );

        // Clear all window flags that might add padding
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_FORCE_NOT_FULLSCREEN);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);

        // Cutout mode
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.P) {
            WindowManager.LayoutParams layoutParams = getWindow().getAttributes();
            layoutParams.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            getWindow().setAttributes(layoutParams);
        }

        // Anonymous WebView subclass so we can intercept startActionMode at
        // the View level — that's where the text-selection floating toolbar
        // (Copy / Share / Select All / Read Aloud) actually originates.
        // Activity.startActionMode overrides do NOT catch it; the request
        // bubbles up the View hierarchy and is handled by the DecorView's
        // local ActionMode provider, bypassing the Activity entirely.
        webView = new WebView(this) {
            @Override
            public ActionMode startActionMode(ActionMode.Callback callback) {
                return super.startActionMode(suppressMenuCallback(callback));
            }
            @Override
            public ActionMode startActionMode(ActionMode.Callback callback, int type) {
                return super.startActionMode(suppressMenuCallback(callback), type);
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
        webView.clearCache(true);

        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient());

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

        webView.loadUrl("http://100.69.184.113:8090");
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
        // Re-add the legacy fullscreen window flag so the status bar stays
        // hidden on older surfaces, then drive the modern InsetsController
        // on API 30+ (Pixel foldables run well past this).
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                c.setSystemBarsBehavior(
                    WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        }
        getWindow().getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        );
    }

    private void releaseImmersive() {
        // Make the system bars visible again so the user can use Android
        // system gestures (swipe-up home, swipe-down notifications, swipe
        // from edge for back). On modern Android the InsetsController is
        // the only thing that actually makes them reappear once
        // FLAG_FULLSCREEN has been set; the legacy SystemUiVisibility flags
        // are ignored. We also clear FLAG_FULLSCREEN itself so the status
        // bar isn't kept hidden by the window-level flag.
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.show(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_DEFAULT);
            }
        }
        // Keep LAYOUT_* flags so the content position doesn't jump when the
        // bars come in/out.
        getWindow().getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
        );
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

    // Suppress Android's default text-selection action bar (Copy / Share /
    // Select All / Read Aloud) so the in-app highlight popup driven by
    // reader.html is the only floating menu the user sees. We return false
    // from onCreateActionMode to prevent the floating toolbar entirely;
    // the underlying selection (and its drag handles) is a separate
    // WebView feature and keeps working. JS detects selectionchange and
    // pops the in-app menu.
    private ActionMode.Callback suppressMenuCallback(final ActionMode.Callback original) {
        return new ActionMode.Callback() {
            @Override
            public boolean onCreateActionMode(ActionMode mode, Menu menu) {
                menu.clear();
                return false;
            }
            @Override
            public boolean onPrepareActionMode(ActionMode mode, Menu menu) {
                menu.clear();
                return false;
            }
            @Override
            public boolean onActionItemClicked(ActionMode mode, MenuItem item) {
                return false;
            }
            @Override
            public void onDestroyActionMode(ActionMode mode) {
                if (original != null) original.onDestroyActionMode(mode);
            }
        };
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
