"""Sprint 4 tests. Real db / diff_engine / reporting; scraper, spec pipeline and Slack are patched."""
import csv
import json
import os
from datetime import datetime, timezone

import pytest

from src import main
from src.models import ScrapedProduct, SpecSheetData, StockStatus
from src.spec_pipeline import SpecResult

URL_A = "https://a.example/p1"
URL_B = "https://b.example/p2"


def _prod(site, url, price=100.0, stock=StockStatus.IN_STOCK, ok=True, pdf="https://x/s.pdf", title=None):
    return ScrapedProduct(
        site_id=site, product_url=url, title=(title if title is not None else f"{site} title") if ok else "",
        price=price if ok else None, currency="USD", stock_status=stock if ok else StockStatus.UNKNOWN,
        pdf_url=pdf, scraped_at=datetime.now(timezone.utc), success=ok,
        error_message=None if ok else "boom")


@pytest.fixture
def env(tmp_path, monkeypatch):
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps([
        {"site_id": "a", "product_url": URL_A, "requires_js": False},
        {"site_id": "b", "product_url": URL_B, "requires_js": False}]))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("TARGETS_CONFIG_PATH", str(targets))
    monkeypatch.setenv("REPORT_OUTPUT_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "run.log"))
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/XXX")
    monkeypatch.setenv("PRICE_CHANGE_THRESHOLD_PCT", "5.0")
    calls = {"slack": [], "spec": []}

    def fake_spec(product_id, pdf_url, settings):
        calls["spec"].append(pdf_url)
        spec = SpecSheetData(warranty_years=3, key_features=["f1"])
        # the real pipeline stores the spec row; the CSV joins on pdf_hash, so mirror that here
        main.db.save_spec_sheet(product_id, pdf_url, "h1", spec, "{}")
        return SpecResult(spec=spec, pdf_hash="h1")

    def fake_slack(diff, product, spec, url):
        calls["slack"].append((diff, product, spec))
        return 200

    monkeypatch.setattr(main.spec_pipeline, "get_or_extract_spec", fake_spec)
    monkeypatch.setattr(main.alerting, "send_slack_alert", fake_slack)
    return tmp_path, calls


def _scrape(monkeypatch, items):
    monkeypatch.setattr(main.scraper, "fetch_all", lambda targets: items)


def _rows(tmp_path):
    with open(tmp_path / "reports" / main.CSV_NAME, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_cold_run_no_alert_and_csv(env, monkeypatch):
    tmp, calls = env
    _scrape(monkeypatch, [_prod("a", URL_A), _prod("b", URL_B)])
    s = main.run_daily_job()
    assert (s.products_checked, s.alerts_sent, s.errors) == (2, 0, [])
    assert calls["slack"] == []
    rows = _rows(tmp)
    assert len(rows) == 2 and rows[0]["warranty_years"] == "3"


def test_price_jump_alerts_once_and_rerun_is_idempotent(env, monkeypatch):
    tmp, calls = env
    _scrape(monkeypatch, [_prod("a", URL_A, 100.0)])
    main.run_daily_job()
    _scrape(monkeypatch, [_prod("a", URL_A, 106.0)])
    s = main.run_daily_job()
    assert s.alerts_sent == 1 and len(calls["slack"]) == 1
    assert calls["slack"][0][2].warranty_years == 3
    s2 = main.run_daily_job()  # same data again: baseline is now 106
    assert s2.alerts_sent == 0 and len(calls["slack"]) == 1


def test_one_failed_product_does_not_abort_run(env, monkeypatch):
    tmp, _ = env
    _scrape(monkeypatch, [_prod("a", URL_A, ok=False), _prod("b", URL_B)])
    s = main.run_daily_job()
    assert s.products_checked == 2
    assert any("scrape failed" in e for e in s.errors)
    rows = {r["site_id"]: r for r in _rows(tmp)}
    assert rows["a"]["scrape_success"] == "0" and "boom" in rows["a"]["error_message"]
    assert rows["b"]["scrape_success"] == "1"


def test_failed_scrape_keeps_stored_title(env, monkeypatch):
    tmp, _ = env
    _scrape(monkeypatch, [_prod("a", URL_A, title="Good Title")])
    main.run_daily_job()
    _scrape(monkeypatch, [_prod("a", URL_A, ok=False)])
    main.run_daily_job()
    assert _rows(tmp)[0]["product_title"] == "Good Title"


def test_exception_in_one_product_is_isolated(env, monkeypatch):
    _scrape(monkeypatch, [_prod("a", URL_A), _prod("b", URL_B)])
    def boom(pid, url, settings):
        if url.endswith("s.pdf") and boom.n == 0:
            boom.n += 1
            raise RuntimeError("kaput")
        return SpecResult(spec=SpecSheetData(), pdf_hash="h")
    boom.n = 0
    monkeypatch.setattr(main.spec_pipeline, "get_or_extract_spec", boom)
    s = main.run_daily_job()
    assert s.products_checked == 2 and any("kaput" in e for e in s.errors)
    assert not any(e.startswith("run failed") for e in s.errors)


def test_slack_failure_not_counted_and_not_logged(env, monkeypatch):
    tmp, calls = env
    _scrape(monkeypatch, [_prod("a", URL_A, 100.0)])
    main.run_daily_job()
    monkeypatch.setattr(main.alerting, "send_slack_alert", lambda *a: 500)
    _scrape(monkeypatch, [_prod("a", URL_A, 110.0)])
    s = main.run_daily_job()
    assert s.alerts_sent == 0 and any("Slack alert failed" in e for e in s.errors)
    assert _rows(tmp)[0]["last_alert_sent_at"] == ""


def test_empty_webhook_reported_once(env, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    _scrape(monkeypatch, [_prod("a", URL_A, 100.0), _prod("b", URL_B, 100.0)])
    main.run_daily_job()
    _scrape(monkeypatch, [_prod("a", URL_A, 200.0), _prod("b", URL_B, 200.0)])
    s = main.run_daily_job()
    assert sum("SLACK_WEBHOOK_URL" in e for e in s.errors) == 1 and s.alerts_sent == 0


def test_fatal_error_returns_summary_and_sends_run_failed(env, monkeypatch):
    sent = []
    monkeypatch.setattr(main.httpx, "post", lambda url, **kw: sent.append(kw["json"]))
    def die(targets):
        raise RuntimeError("network down")
    monkeypatch.setattr(main.scraper, "fetch_all", die)
    s = main.run_daily_job()
    assert any(e.startswith("run failed") for e in s.errors)
    assert len(sent) == 1 and "RuntimeError" in sent[0]["text"]


def test_missing_targets_file_is_fatal_but_soft(env, monkeypatch):
    monkeypatch.setenv("TARGETS_CONFIG_PATH", "/nonexistent/targets.json")
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: None)
    s = main.run_daily_job()
    assert any(e.startswith("run failed") for e in s.errors) and s.products_checked == 0


def test_spec_error_recorded_without_storing_hash(env, monkeypatch):
    monkeypatch.setattr(main.spec_pipeline, "get_or_extract_spec",
                        lambda *a: SpecResult(spec=None, pdf_hash="hh", error="ai failed"))
    _scrape(monkeypatch, [_prod("a", URL_A)])
    s = main.run_daily_job()
    assert any("ai failed" in e for e in s.errors)
    assert _rows(env[0])[0]["warranty_years"] == ""


def test_load_targets(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps([{"site_id": "x", "product_url": "u", "requires_js": True}]))
    t = main.load_targets(str(p))
    assert t[0].site_id == "x" and t[0].requires_js is True
