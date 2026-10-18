/* gr-outbox.js (#370) — durable write queue for reading/listening progress.
 *
 * GROutbox.send(url, opts) tries the request; if the server can't be reached
 * (network error, timeout, 5xx, or GRNet says offline) the request is stored
 * in IndexedDB and replayed FIFO when the server is back. Every request carries
 * a flush_id (uuid; header X-Flush-Id and, for JSON bodies, a body field) so the
 * server can ignore a replay it already applied.
 *
 * Resolves {queued:boolean, ok:boolean, status?:number, response?:Response}.
 */
(function () {
  'use strict';
  if (window.GROutbox) return;

  var DB_NAME = 'GROutbox', STORE = 'queue';
  var dbp = null;
  var flushing = false;
  var countCbs = [];

  function openDB() {
    if (dbp) return dbp;
    dbp = new Promise(function (resolve, reject) {
      if (!window.indexedDB) return reject(new Error('no indexedDB'));
      var rq = indexedDB.open(DB_NAME, 1);
      rq.onupgradeneeded = function () {
        rq.result.createObjectStore(STORE, { autoIncrement: true });
      };
      rq.onsuccess = function () { resolve(rq.result); };
      rq.onerror = function () { reject(rq.error); };
    });
    dbp.catch(function () { dbp = null; });
    return dbp;
  }
  function tx(mode, fn) {
    return openDB().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, mode), out;
        try { out = fn(t.objectStore(STORE)); } catch (e) { reject(e); return; }
        t.oncomplete = function () { resolve(out && out.result !== undefined ? out.result : out); };
        t.onerror = t.onabort = function () { reject(t.error); };
      });
    });
  }

  function uuid() {
    try { if (crypto.randomUUID) return crypto.randomUUID(); } catch (_) {}
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 3 | 8);
      return v.toString(16);
    });
  }

  function count() {
    return tx('readonly', function (s) { return s.count(); }).catch(function () { return 0; });
  }
  function notifyCount() {
    count().then(function (n) {
      countCbs.slice().forEach(function (cb) { try { cb(n); } catch (_) {} });
      if (window.GRNet && GRNet.refreshPending) GRNet.refreshPending();
    });
  }

  function enqueue(rec) {
    return tx('readwrite', function (s) { return s.add(rec); })
      .then(function () { notifyCount(); return true; }, function (e) {
        console.warn('GROutbox: could not persist request', e); return false;
      });
  }

  function isOffline() { return !!(window.GRNet && GRNet.online === false); }

  function doFetch(rec, timeoutMs) {
    var ctl = ('AbortController' in window) ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, timeoutMs || 10000);
    var init = { method: rec.method, headers: rec.headers, body: rec.body, signal: ctl ? ctl.signal : undefined };
    if (rec.keepalive) init.keepalive = true;
    return fetch(rec.url, init).then(function (r) { clearTimeout(timer); return r; },
      function (e) { clearTimeout(timer); throw e; });
  }

  function send(url, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    var fid = opts.flushId || uuid();
    headers['X-Flush-Id'] = fid;
    var body = opts.body;
    if (body && typeof body === 'object' && !(body instanceof Blob) && !(body instanceof FormData) &&
        !(body instanceof ArrayBuffer) && !(body instanceof URLSearchParams)) {
      body = JSON.stringify(body);
    }
    if (typeof body === 'string' && /json/i.test(headers['Content-Type'] || headers['content-type'] || '')) {
      try {
        var o = JSON.parse(body);
        if (o && typeof o === 'object' && !Array.isArray(o)) { o.flush_id = fid; body = JSON.stringify(o); }
      } catch (_) {}
    }
    var rec = {
      url: url, method: (opts.method || 'POST').toUpperCase(), headers: headers,
      body: body, created: Date.now(), kind: opts.kind || 'generic'
    };

    function queue() {
      return enqueue(rec).then(function () { return { queued: true, ok: false }; });
    }
    if (isOffline()) return queue();
    // Page-unload saves must start the request synchronously (an async queue
    // lookup first would never run); the queue still catches a failure if the
    // page survives long enough to see it.
    if (opts.keepalive) {
      rec.keepalive = true;
      return doFetch(rec, opts.timeoutMs).then(function (resp) {
        if (resp.status >= 500) return queue();
        return { queued: false, ok: resp.ok, status: resp.status, response: resp };
      }, function () { delete rec.keepalive; return queue(); });
    }
    // Keep FIFO: if older writes are still waiting, join the line.
    return count().then(function (n) {
      if (n > 0) {
        return queue().then(function (r) { flush(); return r; });
      }
      var p = doFetch(rec, opts.timeoutMs);
      return p.then(function (resp) {
        if (resp.status >= 500) return queue();          // server hiccup: retry later
        return { queued: false, ok: resp.ok, status: resp.status, response: resp };
      }, function () { return queue(); });               // TypeError / AbortError
    });
  }

  // Replay FIFO. Network error / 5xx stop (retry later); 2xx delete; 4xx delete+warn.
  function flush() {
    if (flushing || isOffline()) return Promise.resolve();
    flushing = true;
    return openDB().then(function (db) {
      return new Promise(function (resolve, reject) {
        var items = [], t = db.transaction(STORE, 'readonly');
        var cur = t.objectStore(STORE).openCursor();
        cur.onsuccess = function () {
          var c = cur.result;
          if (c) { items.push({ key: c.key, rec: c.value }); c.continue(); }
        };
        t.oncomplete = function () { resolve(items); };
        t.onerror = t.onabort = function () { reject(t.error); };
      });
    }).then(function (items) {
      var chain = Promise.resolve(true);
      items.forEach(function (it) {
        chain = chain.then(function (go) {
          if (!go) return false;
          return doFetch(it.rec, 15000).then(function (resp) {
            if (resp.status >= 500 || resp.status === 408 || resp.status === 429) return false;
            if (!resp.ok) console.warn('GROutbox: dropping rejected request', resp.status, it.rec.url);
            return tx('readwrite', function (s) { return s.delete(it.key); }).then(function () { return true; });
          }, function () { return false; });        // network error: stop, keep the rest
        });
      });
      return chain;
    }).catch(function (e) { console.warn('GROutbox flush', e); })
      .then(function () { flushing = false; notifyCount(); });
  }

  window.GROutbox = {
    send: send,
    flush: flush,
    count: count,
    onCount: function (cb) { if (typeof cb === 'function') countCbs.push(cb); }
  };

  // Flush on load, whenever the server comes back, and every 30s while online.
  function wire() {
    if (window.GRNet) GRNet.onChange(function (up) { if (up) flush(); });
    flush();
    notifyCount();
    setInterval(function () { if (!isOffline()) flush(); }, 30000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();
