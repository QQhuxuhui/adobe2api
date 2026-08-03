import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from leonardo_refresher.service import RuntimeState


class HealthServer:
    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread):
        self._server = server
        self._thread = thread
        self.port = int(server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _handler_for(state: RuntimeState):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/healthz":
                self.send_error(404)
                return

            snapshot = state.snapshot()
            body = json.dumps(snapshot, separators=(",", ":")).encode(
                "utf-8"
            )
            status = 503 if snapshot["state"] == "browser_unavailable" else 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    return HealthHandler


def start_health_server(
    state: RuntimeState,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> HealthServer:
    server = ThreadingHTTPServer((host, int(port)), _handler_for(state))
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        name="leonardo-health",
        daemon=True,
    )
    thread.start()
    return HealthServer(server, thread)
