#!/usr/bin/env python3
"""
Static file server for the Ereader web reader, drop-in replacement for
`python3 -m http.server 8090`.

Differences from the stdlib server:
  * Sends `Cache-Control: no-store, no-cache, must-revalidate` on every response
    so the Android WebView never serves stale reader.html / index.html.
  * Sets the correct MIME for .apk so a phone browser actually downloads it.
  * Logs to stdout (suppressible with --quiet).

Usage:
    cd /home/brandon/projects/Ereader/web
    python3 serve.py            # binds 0.0.0.0:8090
    python3 serve.py --port 8090 --quiet
"""
import argparse
import http.server
import socketserver
import sys


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".apk": "application/vnd.android.package-archive",
        ".epub": "application/epub+zip",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".html": "text/html; charset=utf-8",
        "": "application/octet-stream",
    }

    # ── GreatReads reverse proxy (Story 1) ───────────────────────────────
    # Serve the vendored GreatReads service same-origin under /greatreads/ so
    # the Android WebView stays on one origin (cookies + history nav). Mirrors
    # the prod nginx config exactly: strip the /greatreads prefix before
    # forwarding, and set X-Forwarded-Prefix so the app's url_for() regenerates
    # /greatreads/... links. WebView never hits the cross-origin :8092 directly.
    GREATREADS_UPSTREAM = "127.0.0.1:8092"

    def _is_greatreads(self):
        p = self.path.split("?", 1)[0]
        return p == "/greatreads" or p.startswith("/greatreads/")

    def _proxy_greatreads(self):
        import http.client
        path_only, _, query = self.path.partition("?")
        # /greatreads (no slash) → redirect to /greatreads/ (matches prod nginx).
        if path_only == "/greatreads":
            self.send_response(301)
            self.send_header("Location", "/greatreads/")
            self.end_headers()
            return
        # Strip the prefix; upstream FastAPI expects unprefixed paths.
        upstream_path = path_only[len("/greatreads"):] or "/"
        if query:
            upstream_path += "?" + query
        self._proxying = True  # tells end_headers() to pass upstream headers through
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            # Forward the original Host (like nginx `proxy_set_header Host $host`)
            # so GreatReads' absolute url_for() links resolve to the client-facing
            # origin (:8090) instead of the upstream (:8092). Strip only true
            # hop-by-hop headers + content-length (http.client recomputes it).
            hop = {"content-length", "connection", "keep-alive",
                   "proxy-authenticate", "proxy-authorization", "te",
                   "trailers", "transfer-encoding", "upgrade"}
            fwd = {k: v for k, v in self.headers.items() if k.lower() not in hop}
            fwd["X-Forwarded-Prefix"] = "/greatreads"
            conn = http.client.HTTPConnection(self.GREATREADS_UPSTREAM, timeout=60)
            conn.request(self.command, upstream_path, body=body, headers=fwd)
            up = conn.getresponse()
            self.send_response(up.status)
            for k, v in up.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "keep-alive"):
                    continue
                self.send_header(k, v)         # keep Set-Cookie, Content-Type, Location, …
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = up.read(65536)     # stream (covers, css, large bodies)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
            conn.close()
        except Exception as e:
            try:
                self.send_error(502, "GreatReads proxy error: %s" % e)
            except Exception:
                pass

    def do_GET(self):
        if self._is_greatreads():
            return self._proxy_greatreads()
        # Launches land on the GreatReads Home page (in-progress + stats, #65).
        if self.path == "/" or self.path == "/index.html":
            self.send_response(302)
            self.send_header("Location", "/greatreads/")
            self.end_headers()
            return
        return super().do_GET()

    def do_HEAD(self):
        if self._is_greatreads():
            return self._proxy_greatreads()
        return super().do_HEAD()

    def do_POST(self):
        if self._is_greatreads():
            return self._proxy_greatreads()
        self.send_error(405, "Method Not Allowed")

    # GreatReads uses PUT/PATCH/DELETE for its CRUD; route them all through.
    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_OPTIONS = do_POST

    def end_headers(self):
        # For proxied GreatReads responses, pass the upstream headers through
        # untouched (don't clobber its Cache-Control / Set-Cookie / Location).
        if not getattr(self, "_proxying", False):
            # Always tell clients (and the WebView's HTTP cache) not to reuse.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            # Permissive CORS so the reader can fetch the API & cover images.
            self.send_header("Access-Control-Allow-Origin", "*")
            # Force the browser to treat .apk as a download (Chrome on Android
            # otherwise tries to "open" it and silently drops it). Has no effect
            # on the in-app WebView since it never requests the APK.
            try:
                path = self.path.split("?", 1)[0].lower()
                if path.endswith(".apk"):
                    self.send_header(
                        "Content-Disposition",
                        'attachment; filename="ereader.apk"',
                    )
                    self.send_header("X-Content-Type-Options", "nosniff")
            except Exception:
                pass
        super().end_headers()


class QuietHandler(NoCacheHandler):
    def log_message(self, *args, **kwargs):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    handler = QuietHandler if args.quiet else NoCacheHandler

    # Allow rebinding to the same port quickly after a restart.
    socketserver.TCPServer.allow_reuse_address = True

    # Threaded so one slow/stalled client (e.g. a phone that opened a request
    # and went away mid-transfer) can't block every other request. A single
    # blocking client on the old single-threaded TCPServer would hang the whole
    # server: the port stayed LISTENing but nothing ever responded, and the
    # port-only watchdog never noticed. daemon_threads => stuck workers don't
    # keep the process alive on shutdown.
    class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    with ThreadingServer((args.bind, args.port), handler) as httpd:
        print(f"Ereader web server: http://{args.bind}:{args.port}  (no-cache)",
              file=sys.stderr, flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.", file=sys.stderr)


if __name__ == "__main__":
    main()
