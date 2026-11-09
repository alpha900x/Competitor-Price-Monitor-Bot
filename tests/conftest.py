import functools
import http.server
import os
import threading

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
SAMPLE_PDF = os.path.join(FIXTURES, "sample_spec_sheet.pdf")


@pytest.fixture
def pdf_server(monkeypatch):
    """Serve tests/fixtures over real HTTP on localhost; returns base URL.

    Extra routes: /landing.html (HTML page, not a PDF), /flaky.pdf (503 once, then PDF).
    """
    state = {"flaky_hits": 0}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/landing.html":
                body = b"<html><body>Download PDF here</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/flaky.pdf":
                state["flaky_hits"] += 1
                if state["flaky_hits"] == 1:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.path = "/sample_spec_sheet.pdf"
                super().do_GET()
            else:
                super().do_GET()

    handler = functools.partial(Handler, directory=FIXTURES)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
