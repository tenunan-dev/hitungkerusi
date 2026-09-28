#!/usr/bin/env python3
"""Static file server for the GE16 Graph Explorer with Cache-Control: no-store.

Why: python3 -m http.server sends NO cache headers, so Safari aggressively
caches index.html/js/json and keeps showing a STALE version of the app after
an update (the "different index.html / no loading page" bug). This server
forces no-store so the browser always fetches the latest files.

Usage:  python3 serve_no_cache.py [port]   (default 8765, binds 127.0.0.1)
"""
import http.server
import socketserver
import sys
import os

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
DIR = os.path.dirname(os.path.abspath(__file__))


class NoCacheServer(socketserver.TCPServer):
    # Without this, a restart right after Stop fails with EADDRINUSE while the
    # previous socket sits in TIME_WAIT (~60s). allow_reuse_address lets the
    # server rebind immediately.
    allow_reuse_address = True


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def end_headers(self):
        # force the browser to always re-fetch — kills stale-cache bugs
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("  [%s] %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    os.chdir(DIR)
    with NoCacheServer(("127.0.0.1", PORT), NoCacheHandler) as httpd:
        print(f"Serving {DIR} on http://localhost:{PORT} (no-cache)", flush=True)
        httpd.serve_forever()
