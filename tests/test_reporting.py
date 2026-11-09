import csv

import pytest

from src import db, reporting


def snap(**kw):
    base = {
        "product_id": 1, "site_id": "router_switch", "product_title": "Cisco C9300-24P-A",
        "product_url": "https://x/p", "price": 2522.0, "currency": "USD", "stock_status": "in_stock",
        "scrape_success": 1, "error_message": None, "pdf_url": "https://x/p.pdf",
        "last_scraped_at": "2026-11-07T06:00:00+00:00", "warranty_years": 3,
        "power_consumption_watts": None, "key_features": ["PoE+", "Stackable"],
        "last_alert_sent_at": "2026-11-06T06:00:00+00:00",
    }
    base.update(kw)
    return base


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def test_columns_and_values(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "get_all_latest_snapshots", lambda: [snap()])
    out = reporting.export_csv(str(tmp_path / "summary_report.csv"))
    assert out == str(tmp_path / "summary_report.csv")
    rows = read(out)
    assert list(rows[0].keys()) == reporting.COLUMNS
    assert reporting.COLUMNS[:10] == [
        "site_id", "product_title", "price", "currency", "stock_status", "warranty_years",
        "power_consumption_watts", "key_features", "last_scraped_at", "last_alert_sent_at",
    ]
    r = rows[0]
    assert r["site_id"] == "router_switch" and r["price"] == "2522.0" and r["currency"] == "USD"
    assert r["warranty_years"] == "3" and r["power_consumption_watts"] == ""
    assert r["key_features"] == "PoE+ | Stackable"
    assert r["last_alert_sent_at"].startswith("2026-11-06")
    assert r["scrape_success"] == "1" and r["error_message"] == ""


def test_failed_scrape_and_missing_spec_rows(tmp_path, monkeypatch):
    failed = snap(site_id="lenovo_in", price=None, currency="INR", stock_status="unknown", scrape_success=0,
                  error_message="HTTP 503", warranty_years=None, key_features=None, last_alert_sent_at=None)
    monkeypatch.setattr(db, "get_all_latest_snapshots", lambda: [snap(), failed])
    rows = read(reporting.export_csv(str(tmp_path / "r.csv")))
    assert len(rows) == 2
    r = rows[1]
    assert r["price"] == "" and r["scrape_success"] == "0" and r["error_message"] == "HTTP 503"
    assert r["key_features"] == "" and r["last_alert_sent_at"] == ""


def test_empty_db_writes_header_only(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "get_all_latest_snapshots", lambda: [])
    p = reporting.export_csv(str(tmp_path / "e.csv"))
    assert read(p) == []
    with open(p, encoding="utf-8-sig") as fh:
        assert fh.readline().strip().split(",") == reporting.COLUMNS


def test_creates_missing_dir_and_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "get_all_latest_snapshots", lambda: [snap()])
    p = str(tmp_path / "nested" / "dir" / "summary_report.csv")
    reporting.export_csv(p)
    monkeypatch.setattr(db, "get_all_latest_snapshots", lambda: [snap(), snap(site_id="b")])
    reporting.export_csv(p)
    assert len(read(p)) == 2
    assert [f.name for f in (tmp_path / "nested" / "dir").iterdir()] == ["summary_report.csv"]  # no temp left


def test_formula_injection_guard_and_unicode(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "get_all_latest_snapshots",
                        lambda: [snap(product_title="=HYPERLINK(\"http://evil\")", key_features=["Cisco™ 10Gb ✓"])])
    r = read(reporting.export_csv(str(tmp_path / "x.csv")))[0]
    assert r["product_title"].startswith("'=")
    assert r["key_features"] == "Cisco™ 10Gb ✓"


def test_failed_write_leaves_no_partial_file(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_all_latest_snapshots", boom)
    with pytest.raises(RuntimeError):
        reporting.export_csv(str(tmp_path / "f.csv"))
    assert list(tmp_path.iterdir()) == []
