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

    def end_headers(self):
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
