package com.ereader.simple;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.support.v4.media.MediaMetadataCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;

import androidx.core.app.NotificationCompat;
import androidx.media.session.MediaButtonReceiver;

import java.net.HttpURLConnection;
import java.net.URL;

// Foreground service that keeps the WebView audiobook playing in the background
// and bridges hardware/headphone + lock-screen media buttons into the player.
// The audio itself stays in the WebView's <audio>; this service only (a) holds
// a MediaSession whose callbacks are forwarded to JS via MainActivity, and
// (b) posts an ongoing MediaStyle notification so Android won't freeze the
// process (which otherwise stalls hls.js segment fetches after a few minutes).
//
// NOTE: this service deliberately does NOT request audio focus. The WebView's
// <audio> element already owns system audio focus for the playback; if the
// service also requested AUDIOFOCUS_GAIN the two would fight over it and the
// WebView would immediately pause (play-then-instant-stop).
public class PlaybackService extends Service {

    public static final String CHANNEL_ID = "greatreads_playback";
    public static final int NOTIF_ID = 1001;
    public static final String ACTION_START  = "com.ereader.simple.media.START";
    public static final String ACTION_UPDATE = "com.ereader.simple.media.UPDATE";
    public static final String ACTION_STOP   = "com.ereader.simple.media.STOP";

    // Same-process handle so MainActivity's JsBridge can push state / stop the
    // service directly instead of re-issuing startForegroundService() — which
    // Android 12+ forbids from the background (i.e. once the screen is locked).
    private static PlaybackService sInstance;

    private MediaSessionCompat session;
    // PARTIAL_WAKE_LOCK: a foreground service alone still gets Dozed after a few
    // minutes with the screen off, which throttles the WebView's JS thread and
    // stalls hls.js segment fetches (audio dies while <audio> still reads as
    // "playing"). The wake lock keeps the CPU alive so playback continues.
    private PowerManager.WakeLock wakeLock;
    private boolean playing = false;
    private long positionMs = 0, durationMs = 0;
    private float rate = 1f;
    private String title = "", artist = "", coverUrl = "", loadedCoverUrl = "";
    private Bitmap coverBmp = null;

    @Override public IBinder onBind(Intent intent) { return null; }

    static boolean isRunning() { return sInstance != null; }

    // Called (on the UI thread) from MainActivity.JsBridge while the service is
    // already running, so we never trigger a background FGS start.
    static void applyState(boolean p, double posSec, double durSec, double r) {
        PlaybackService s = sInstance;
        if (s != null) s.onStateUpdate(p, posSec, durSec, r);
    }

    static void stopFromBridge() {
        PlaybackService s = sInstance;
        if (s != null) s.stopPlayback();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        MainActivity.NativeDiag.installCrashHandler(this);   // #337: the service can outlive the activity
        sInstance = this;
        createChannel();
        session = new MediaSessionCompat(this, "GreatReads");
        session.setCallback(new MediaSessionCompat.Callback() {
            @Override public void onPlay()  { MainActivity.dispatchMedia("play"); }
            @Override public void onPause() { MainActivity.dispatchMedia("pause"); }
            @Override public void onStop()  { MainActivity.dispatchMedia("pause"); }
            @Override public void onSkipToNext()     { MainActivity.dispatchMedia("next"); }
            @Override public void onSkipToPrevious() { MainActivity.dispatchMedia("prev"); }
            @Override public void onFastForward()    { MainActivity.dispatchMedia("forward"); }
            @Override public void onRewind()         { MainActivity.dispatchMedia("backward"); }
            @Override public void onSeekTo(long pos) { MainActivity.dispatchMedia("seek:" + pos); }
        });
        session.setActive(true);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) return START_NOT_STICKY;
        MediaButtonReceiver.handleIntent(session, intent);  // headphone keys while backgrounded
        String action = intent.getAction();
        if (ACTION_STOP.equals(action)) { stopPlayback(); return START_NOT_STICKY; }
        if (ACTION_START.equals(action) || ACTION_UPDATE.equals(action)) {
            if (intent.hasExtra("title"))    title    = str(intent.getStringExtra("title"));
            if (intent.hasExtra("artist"))   artist   = str(intent.getStringExtra("artist"));
            if (intent.hasExtra("coverUrl")) coverUrl = str(intent.getStringExtra("coverUrl"));
            playing    = intent.getBooleanExtra("playing", playing);
            positionMs = (long) (intent.getDoubleExtra("position", positionMs / 1000.0) * 1000);
            durationMs = (long) (intent.getDoubleExtra("duration", durationMs / 1000.0) * 1000);
            rate       = (float) intent.getDoubleExtra("rate", rate);
            updateWakeLock();
            updateSession();
            startForeground(NOTIF_ID, buildNotification());
            maybeLoadCover();
        }
        return START_NOT_STICKY;
    }

    // Direct (same-process) state push from the JsBridge while running, so we
    // refresh the session + ongoing notification without a background FGS start.
    void onStateUpdate(boolean p, double posSec, double durSec, double r) {
        playing    = p;
        positionMs = (long) (posSec * 1000);
        durationMs = (long) (durSec * 1000);
        rate       = (float) r;
        updateWakeLock();
        updateSession();
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) nm.notify(NOTIF_ID, buildNotification());
    }

    private void updateSession() {
        long actions = PlaybackStateCompat.ACTION_PLAY | PlaybackStateCompat.ACTION_PAUSE
                | PlaybackStateCompat.ACTION_PLAY_PAUSE | PlaybackStateCompat.ACTION_STOP
                | PlaybackStateCompat.ACTION_SKIP_TO_NEXT | PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS
                | PlaybackStateCompat.ACTION_FAST_FORWARD | PlaybackStateCompat.ACTION_REWIND
                | PlaybackStateCompat.ACTION_SEEK_TO;
        session.setPlaybackState(new PlaybackStateCompat.Builder().setActions(actions)
                .setState(playing ? PlaybackStateCompat.STATE_PLAYING
                                  : PlaybackStateCompat.STATE_PAUSED, positionMs, rate).build());
        MediaMetadataCompat.Builder md = new MediaMetadataCompat.Builder()
                .putString(MediaMetadataCompat.METADATA_KEY_TITLE, title)
                .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, artist)
                .putLong(MediaMetadataCompat.METADATA_KEY_DURATION, durationMs);
        if (coverBmp != null) md.putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, coverBmp);
        session.setMetadata(md.build());
    }

    private Notification buildNotification() {
        NotificationCompat.Builder b = new NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle(title).setContentText(artist)
                .setContentIntent(contentIntent()).setOnlyAlertOnce(true).setOngoing(playing)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC);
        if (coverBmp != null) b.setLargeIcon(coverBmp);
        b.addAction(act(android.R.drawable.ic_media_previous, "Prev", PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS));
        b.addAction(act(android.R.drawable.ic_media_rew, "Back 30s", PlaybackStateCompat.ACTION_REWIND));
        b.addAction(playing
                ? act(android.R.drawable.ic_media_pause, "Pause", PlaybackStateCompat.ACTION_PLAY_PAUSE)
                : act(android.R.drawable.ic_media_play, "Play", PlaybackStateCompat.ACTION_PLAY_PAUSE));
        b.addAction(act(android.R.drawable.ic_media_ff, "Fwd 30s", PlaybackStateCompat.ACTION_FAST_FORWARD));
        b.addAction(act(android.R.drawable.ic_media_next, "Next", PlaybackStateCompat.ACTION_SKIP_TO_NEXT));
        b.setStyle(new androidx.media.app.NotificationCompat.MediaStyle()
                .setMediaSession(session.getSessionToken())
                .setShowActionsInCompactView(1, 2, 3));  // Back, Play/Pause, Fwd
        return b.build();
    }

    private NotificationCompat.Action act(int icon, String label, long action) {
        return new NotificationCompat.Action(icon, label,
                MediaButtonReceiver.buildMediaButtonPendingIntent(this, action));
    }

    private PendingIntent contentIntent() {
        Intent i = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        return PendingIntent.getActivity(this, 0, i, flags);
    }

    // ---- Wake lock (CPU stays awake so background playback doesn't stall) ----
    private void updateWakeLock() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm == null) return;
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "GreatReads:playback");
            wakeLock.setReferenceCounted(false);
        }
        if (playing) { if (!wakeLock.isHeld()) wakeLock.acquire(); }
        else if (wakeLock.isHeld()) wakeLock.release();
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
    }

    private void stopPlayback() {
        releaseWakeLock();
        if (session != null) session.setActive(false);
        if (Build.VERSION.SDK_INT >= 24) stopForeground(Service.STOP_FOREGROUND_REMOVE);
        else stopForeground(true);
        stopSelf();
    }

    // Fetch the cover off the main thread; refresh the session + notification
    // once it arrives so the lock screen shows the audiobook art.
    private void maybeLoadCover() {
        if (coverUrl == null || coverUrl.isEmpty() || coverUrl.equals(loadedCoverUrl)) return;
        final String url = coverUrl;
        loadedCoverUrl = url;
        new Thread(() -> {
            Bitmap bmp = null;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(5000);
                c.setReadTimeout(5000);
                bmp = BitmapFactory.decodeStream(c.getInputStream());
                c.disconnect();
            } catch (Exception ignored) {}
            if (bmp != null) {
                coverBmp = bmp;
                updateSession();
                NotificationManager nm =
                        (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (nm != null) nm.notify(NOTIF_ID, buildNotification());
            }
        }).start();
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "Playback",
                    NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            NotificationManager nm =
                    (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.createNotificationChannel(ch);
        }
    }

    private static String str(String s) { return s == null ? "" : s; }

    @Override
    public void onDestroy() {
        if (sInstance == this) sInstance = null;
        releaseWakeLock();
        if (session != null) { session.setActive(false); session.release(); }
        super.onDestroy();
    }
}
