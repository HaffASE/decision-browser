"""Owned loopback counter page for driver tests. Does not touch a user browser."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_provider_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never let tests or inherited subprocesses consult a real user profile."""
    path = tmp_path / "user-config" / "provider.json"
    monkeypatch.setenv("DECISION_BROWSER_CONFIG_FILE", str(path))
    return path

COUNTER_PAGE_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>decision-browser-counter</title></head>
<body>
<p id="count">0</p>
<button id="inc" type="button">inc</button>
<button id="reset" type="button">reset</button>
<script>
const count = document.getElementById("count");
let n = 0;
document.getElementById("inc").addEventListener("click", () => {
  n += 1;
  count.textContent = String(n);
});
document.getElementById("reset").addEventListener("click", () => {
  n = 0;
  count.textContent = "0";
});
</script>
</body>
</html>
"""


class _QuietHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = COUNTER_PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def counter_page_url() -> Iterator[str]:
    """Serve the counter page on 127.0.0.1 and stop the server afterwards."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
