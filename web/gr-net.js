/* gr-net.js (#370) — one "is the SERVER reachable?" signal for every page.
 *
 * Android's "has internet" is true on Wi-Fi/LTE with Tailscale off, so pages
 * can't trust navigator.onLine. GRNet probes the API container directly and
 * exposes {online, onChange(cb)}. It also toggles html.gr-offline and injects
 * a banner: "Offline — your reading is saved on this phone and will sync when
 * you're back · N waiting to sync" (N from GROutbox.count()).
 *
 * Served at /gr-net.js by :8090 and bundled in the APK at the same path.
 */
(function () {
  'use strict';
  if (window.GRNet) return;

  var HOST = (location.hostname && /^\d+\.\d+\.\d+\.\d+$|\./.test(location.hostname))
    ? location.hostname : '100.69.184.113';
  var HEALTH = 'http://' + HOST + ':8092/api/health';
  var PROBE_MS = 15000, PROBE_OFFLINE_MS = 5000, TIMEOUT_MS = 4000, FAILS_TO_OFFLINE = 2;
  var fails = 0, timerId = null;
  var root = document.documentElement;
  var listeners = [];
  var probing = false;
  var started = false;

  // The Android shell tags cached/bundled pages gr-offline before any JS runs;
  // believe it until the first probe answers.
  var GRNet = { online: !root.classList.contains('gr-offline') };

  GRNet.onChange = function (cb) {
    if (typeof cb !== 'function') return function () {};
    listeners.push(cb);
    return function () { var i = listeners.indexOf(cb); if (i >= 0) listeners.splice(i, 1); };
  };

  function setOnline(v) {
    v = !!v;
    var changed = v !== GRNet.online;
    GRNet.online = v;
    root.classList.toggle('gr-offline', !v);
    renderBanner();
    if (changed) {
      listeners.slice().forEach(function (cb) { try { cb(v); } catch (e) { console.warn('GRNet listener', e); } });
    }
  }

  // Probe via CORS (the API allows *): a real server answers with CORS headers
  // (any status = up); the Android shell's synthetic offline 504 and a dead
  // network both reject, so neither is mistaken for "up".
  GRNet.probe = function () {
    if (probing) return Promise.resolve(GRNet.online);
    probing = true;
    var ctl = ('AbortController' in window) ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, TIMEOUT_MS);
    return fetch(HEALTH + '?_=' + Date.now(), { mode: 'cors', cache: 'no-store', signal: ctl ? ctl.signal : undefined })
      .then(function () { return true; }, function () { return false; })
      .then(function (up) {
        clearTimeout(timer); probing = false;
        // Online at once on success; offline only after consecutive failures,
        // so one network blip never shows the banner.
        if (up) { fails = 0; setOnline(true); }
        else { fails++; if (fails >= FAILS_TO_OFFLINE) setOnline(false); }
        schedule();
        return up;
      });
  };

  // ---- banner ----------------------------------------------------------
  var bannerEl = null, pending = 0;
  function ensureBanner() {
    if (bannerEl || !document.body) return bannerEl;
    var st = document.createElement('style');
    st.textContent =
      '#gr-net-banner{display:none;position:fixed;left:0;right:0;top:0;z-index:2147483000;' +
      'background:#b45309;color:#fff;font:600 13px/1.35 system-ui,sans-serif;' +
      'padding:calc(6px + env(safe-area-inset-top,0px)) 12px 6px;text-align:center;pointer-events:none}' +
      'html.gr-offline #gr-net-banner{display:block}' +
      'html.gr-offline body{padding-top:0}';
    document.head.appendChild(st);
    bannerEl = document.createElement('div');
    bannerEl.id = 'gr-net-banner';
    bannerEl.setAttribute('role', 'status');
    document.body.appendChild(bannerEl);
    return bannerEl;
  }
  function renderBanner() {
    var el = ensureBanner();
    if (!el) return;
    el.textContent = "Offline — your reading is saved on this phone and will sync when you're back" +
      (pending > 0 ? ' · ' + pending + ' waiting to sync' : '');
  }
  function refreshPending() {
    if (window.GROutbox && GROutbox.count) {
      GROutbox.count().then(function (n) { pending = n || 0; renderBanner(); }, function () {});
    }
  }
  GRNet.refreshPending = refreshPending;

  // 15s while healthy; 5s after a failure or while offline (confirm blips, recover fast).
  function schedule() {
    if (timerId) clearTimeout(timerId);
    timerId = setTimeout(function () { GRNet.probe(); refreshPending(); },
      (!GRNet.online || fails > 0) ? PROBE_OFFLINE_MS : PROBE_MS);
  }

  function start() {
    if (started) return; started = true;
    renderBanner();
    refreshPending();
    GRNet.probe();
    schedule();
    window.addEventListener('online', function () { GRNet.probe(); });
    // Radio dropped: confirm with a probe (two failures needed), don't flip on the event alone.
    window.addEventListener('offline', function () { GRNet.probe(); });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') GRNet.probe();
    });
  }

  window.GRNet = GRNet;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
