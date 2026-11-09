import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.alerting import NOT_SENT, build_slack_payload, format_price, send_slack_alert
from src.diff_engine import compute_diff
from src.models import ScrapedProduct, SpecSheetData, StockStatus


def product(price=2672.0, currency="USD", stock=StockStatus.IN_STOCK, title="Cisco C9300-24P-A"):
    return ScrapedProduct(
        site_id="router_switch", product_url="https://www.router-switch.com/c9300-24p-a.html",
        title=title, price=price, currency=currency, stock_status=stock,
        scraped_at=datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc), success=True,
    )


def prev(price, stock="in_stock", currency="USD"):
    return {"price": price, "stock_status": stock, "scrape_success": 1, "currency": currency}


def flat(payload):
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(
    "amount,currency,expected",
    [
        (2522.0, "USD", "$2,522.00"),
        (951033.0, "INR", "₹9,51,033.00"),
        (12345678.5, "INR", "₹1,23,45,678.50"),
        (999.0, "INR", "₹999.00"),
        (1234.5, "EUR", "€1,234.50"),
        (10.0, "CHF", "CHF 10.00"),
        (None, "USD", "n/a"),
    ],
)
def test_format_price(amount, currency, expected):
    assert format_price(amount, currency) == expected


def test_price_alert_payload_usd():
    p = product(2672.0)
    d = compute_diff(1, p, prev(2520.0), 5.0)  # +6.03%
    payload = build_slack_payload(d, p, None)
    text = flat(payload)
    assert payload["blocks"][0]["text"]["text"] == "Price change"
    assert "$2,520.00 → $2,672.00 (+6.0%)" in text
    assert "(unchanged)" in text  # stock line
    assert "Warranty" not in text
    assert "router_switch" in text and p.product_url in text


def test_price_alert_payload_inr_not_dollar():
    p = product(1008095.0, currency="INR")
    d = compute_diff(1, p, prev(951033.0, currency="INR"), 5.0)
    text = flat(build_slack_payload(d, p, None))
    assert "₹9,51,033.00 → ₹10,08,095.00" in text
    assert "$" not in text


def test_stock_only_alert_payload():
    p = product(2520.0, stock=StockStatus.OUT_OF_STOCK)
    d = compute_diff(1, p, prev(2520.0), 5.0)
    payload = build_slack_payload(d, p, None)
    text = flat(payload)
    assert payload["blocks"][0]["text"]["text"] == "Stock change"
    assert "In stock → Out of stock" in text
    assert "below alert threshold" in text  # price shown for context, 0.0% delta


def test_both_and_spec_highlights():
    p = product(2800.0, stock=StockStatus.BACKORDER)
    d = compute_diff(1, p, prev(2520.0), 5.0)
    spec = SpecSheetData(warranty_years=3, power_consumption_watts=445,
                         key_features=[f"feature {i}" for i in range(9)])
    payload = build_slack_payload(d, p, spec)
    text = flat(payload)
    assert payload["blocks"][0]["text"]["text"] == "Price & stock change"
    assert "*Warranty:* 3 year(s)" in text and "*Power:* 445 W" in text
    assert "feature 4" in text and "feature 5" not in text  # capped at 5


def test_spec_with_all_nulls_adds_no_spec_block():
    p = product(2800.0)
    d = compute_diff(1, p, prev(2520.0), 5.0)
    with_spec = build_slack_payload(d, p, SpecSheetData())
    without = build_slack_payload(d, p, None)
    assert len(with_spec["blocks"]) == len(without["blocks"])


def test_title_is_escaped_for_slack():
    p = product(2800.0, title="Switch <b>&</b> more")
    d = compute_diff(1, p, prev(2520.0), 5.0)
    text = flat(build_slack_payload(d, p, None))
    assert "&lt;b&gt;&amp;&lt;/b&gt;" in text


def test_payload_is_json_serialisable_and_blocks_valid():
    p = product(2800.0)
    d = compute_diff(1, p, prev(2520.0), 5.0)
    payload = build_slack_payload(d, p, SpecSheetData(warranty_years=1))
    json.dumps(payload)
    assert {b["type"] for b in payload["blocks"]} <= {"header", "section", "context"}
    assert payload["text"]


# ---- real HTTP round-trip against a local fake Slack webhook ----
class _Handler(BaseHTTPRequestHandler):
    status = 200
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append((self.path, json.loads(body)))
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"ok" if type(self).status == 200 else b"nope")

    def log_message(self, *a):
        pass


@pytest.fixture
def slack(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    _Handler.status, _Handler.received = 200, []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield _Handler, f"http://127.0.0.1:{srv.server_address[1]}/services/T/B/X"
    srv.shutdown()


def _alert_inputs():
    p = product(2800.0)
    return compute_diff(1, p, prev(2520.0), 5.0), p


def test_send_posts_payload_and_returns_200(slack):
    handler, url = slack
    d, p = _alert_inputs()
    assert send_slack_alert(d, p, SpecSheetData(warranty_years=3), url) == 200
    assert len(handler.received) == 1
    path, body = handler.received[0]
    assert path == "/services/T/B/X"
    assert body["blocks"][0]["text"]["text"] == "Price change"


def test_send_returns_non_200_status_without_raising(slack):
    handler, url = slack
    handler.status = 500
    d, p = _alert_inputs()
    assert send_slack_alert(d, p, None, url) == 500


def test_send_empty_webhook_is_soft_noop():
    d, p = _alert_inputs()
    assert send_slack_alert(d, p, None, "") == NOT_SENT


def test_send_unreachable_host_is_soft_failure():
    d, p = _alert_inputs()
    assert send_slack_alert(d, p, None, "http://127.0.0.1:9/hook") == NOT_SENT
