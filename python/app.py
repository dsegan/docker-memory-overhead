"""Minimal stdlib HTTP server: GET /api/hello -> "Hello World"."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = b"Hello World"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hello/1.0"

    def do_GET(self):  # noqa: N802
        if self.path != "/api/hello":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"python/http.server listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
