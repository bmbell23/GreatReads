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

        webView = new WebView(this);
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
        // system DownloadManager instead of silently dropping it.
        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent,
                                        String contentDisposition,
                                        String mimetype, long contentLength) {
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
    // reader.html is the only floating menu the user sees. We wrap the
    // WebView's callback rather than returning null so the selection
    // handles and the underlying selection itself still work — we just
    // strip every item from the menu before the system can render it.
    @Override
    public ActionMode startActionMode(ActionMode.Callback callback, int type) {
        return super.startActionMode(wrapToSuppressMenu(callback), type);
    }

    @Override
    public ActionMode startActionMode(ActionMode.Callback callback) {
        return super.startActionMode(wrapToSuppressMenu(callback));
    }

    private ActionMode.Callback wrapToSuppressMenu(final ActionMode.Callback original) {
        return new ActionMode.Callback() {
            @Override
            public boolean onCreateActionMode(ActionMode mode, Menu menu) {
                boolean ok = original.onCreateActionMode(mode, menu);
                menu.clear();
                return ok;
            }
            @Override
            public boolean onPrepareActionMode(ActionMode mode, Menu menu) {
                original.onPrepareActionMode(mode, menu);
                menu.clear();
                return true;
            }
            @Override
            public boolean onActionItemClicked(ActionMode mode, MenuItem item) {
                return original.onActionItemClicked(mode, item);
            }
            @Override
            public void onDestroyActionMode(ActionMode mode) {
                original.onDestroyActionMode(mode);
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
