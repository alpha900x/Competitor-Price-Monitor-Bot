"""Sprint 5 QA: two full runs in one day. Real main/db/diff_engine/alerting/reporting;
only the scraper and spec pipeline are patched, and Slack is a real local HTTP server."""
import http.server
import json
import threading
from datetime import datetime, timezone

import pytest

from src import main
from src.models import ScrapedProduct, StockStatus

URL = "https://a.example/p1"


def _prod(price, stock=StockStatus.IN_STOCK):
    return ScrapedProduct(site_id="a", product_url=URL, title="A", price=price, currency="USD",
                          stock_status=stock, pdf_url=None, scraped_at=datetime.now(timezone.utc),
                          success=True)


@pytest.fixture
def slack(tmp_path, monkeypatch):
    hits = []
    code = {"value": 200}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(code["value"])
            self.end_headers()
            self.wfile.write(b"ok" if code["value"] == 200 else b"nope")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    targets = tmp_path / "t.json"
    targets.write_text(json.dumps([{"site_id": "a", "product_url": URL, "requires_js": False}]))
    for k, v in {"DB_PATH": tmp_path / "p.db", "TARGETS_CONFIG_PATH": targets,
                 "REPORT_OUTPUT_DIR": tmp_path / "r", "LOG_FILE_PATH": tmp_path / "run.log",
                 "SLACK_WEBHOOK_URL": f"http://127.0.0.1:{srv.server_address[1]}/hook",
                 "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}.items():
        monkeypatch.setenv(k, str(v))
    yield hits, code
    srv.shutdown()


def _run(monkeypatch, product):
    monkeypatch.setattr(main.scraper, "fetch_all", lambda t: [product])
    return main.run_daily_job()


def test_two_runs_same_day_send_exactly_one_slack_message(slack, monkeypatch):
    hits, _ = slack
    assert _run(monkeypatch, _prod(100.0)).alerts_sent == 0      # baseline
    assert _run(monkeypatch, _prod(106.0)).alerts_sent == 1      # +6% -> alert
    assert _run(monkeypatch, _prod(106.0)).alerts_sent == 0      # re-run, unchanged
    assert len(hits) == 1
    assert "6.0%" in json.dumps(hits[0])
    assert len(main.db.get_all_latest_snapshots()) == 1


def test_stock_change_alerts_once_across_reruns(slack, monkeypatch):
    hits, _ = slack
    _run(monkeypatch, _prod(100.0))
    _run(monkeypatch, _prod(100.0, StockStatus.OUT_OF_STOCK))
    _run(monkeypatch, _prod(100.0, StockStatus.OUT_OF_STOCK))
    assert len(hits) == 1


def test_failed_slack_send_is_not_logged_and_not_retried(slack, monkeypatch):
    """Documents the accepted trade-off: snapshot is saved, so no retry next run."""
    hits, code = slack
    _run(monkeypatch, _prod(100.0))
    code["value"] = 500
    s = _run(monkeypatch, _prod(110.0))
    assert s.alerts_sent == 0 and any("status 500" in e for e in s.errors)
    s2 = _run(monkeypatch, _prod(110.0))
    assert s2.alerts_sent == 0 and len(hits) == 1
    assert main.db.get_all_latest_snapshots()[0]["last_alert_sent_at"] is None
