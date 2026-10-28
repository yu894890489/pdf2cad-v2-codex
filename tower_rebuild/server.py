from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".svg": "image/svg+xml",
                      ".js": "text/javascript", ".json": "application/json", ".css": "text/css"}

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_request(self, code="-", size="-"):
        # A gallery loads many small crops. Keep background stdout/stderr pipes
        # from filling with successful requests; retain HTTP error diagnostics.
        if isinstance(code, int) and code >= 400:
            super().log_request(code, size)


class LocalHTTPServer(ThreadingHTTPServer):
    # A fresh page can open several connections while crop requests are queued.
    request_queue_size = 128


def serve(run_dir, port=8766):
    root = (Path(run_dir) / "site").resolve()
    if not (root / "index.html").exists():
        raise ValueError("请先运行 build 生成页面。")
    server = LocalHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(root)))
    print(f"Local viewer: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
