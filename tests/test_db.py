import sqlite3
from datetime import datetime, timezone

import pytest

from src import db
from src.config import get_settings
from src.models import ScrapedProduct, SpecSheetData, StockStatus


@pytest.fixture()
def db_file(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    return path


def _scraped(price=100.0, stock=StockStatus.IN_STOCK, pdf_url=None, ok=True):
    return ScrapedProduct(
        site_id="s1", product_url="https://x/p1", title="Widget", price=price,
        currency="USD", stock_status=stock, pdf_url=pdf_url,
        scraped_at=datetime.now(timezone.utc), success=ok,
        error_message=None if ok else "boom",
    )


def test_init_creates_all_tables(db_file):
    conn = sqlite3.connect(db_file)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"products", "snapshots", "spec_sheets", "alerts_log"} <= names
    cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    assert {"product_id", "scraped_at", "price", "currency", "stock_status", "pdf_url",
            "pdf_hash", "scrape_success", "error_message"} <= cols
    conn.close()


def test_init_is_idempotent(db_file):
    db.init_db(db_file)


def test_product_upsert_and_snapshot_roundtrip(db_file):
    pid = db.upsert_product("s1", "https://x/p1", "Widget")
    assert db.upsert_product("s1", "https://x/p1", "Widget v2") == pid
    assert db.get_last_snapshot(pid) is None
    sid = db.save_snapshot(pid, _scraped(), None)
    last = db.get_last_snapshot(pid)
    assert last["id"] == sid and last["price"] == 100.0
    assert last["stock_status"] == "in_stock" and last["scrape_success"] == 1
    db.save_snapshot(pid, _scraped(price=106.0), None)
    assert db.get_last_snapshot(pid)["price"] == 106.0


def test_spec_sheet_by_hash_and_dedupe(db_file):
    pid = db.upsert_product("s1", "https://x/p1", "Widget")
    spec = SpecSheetData(warranty_years=3, power_consumption_watts=750, key_features=["a", "b"])
    assert db.get_spec_sheet_by_hash("h1") is None
    id1 = db.save_spec_sheet(pid, "https://x/a.pdf", "h1", spec, "{}")
    assert db.save_spec_sheet(pid, "https://x/a.pdf", "h1", spec, "{}") == id1
    got = db.get_spec_sheet_by_hash("h1")
    assert got["warranty_years"] == 3 and got["key_features"] == ["a", "b"]


def test_alert_log_and_latest_snapshots(db_file):
    pid = db.upsert_product("s1", "https://x/p1", "Widget")
    spec = SpecSheetData(warranty_years=5, key_features=["x"])
    db.save_spec_sheet(pid, "https://x/a.pdf", "h1", spec, "{}")
    db.save_snapshot(pid, _scraped(price=90.0), None)
    sid = db.save_snapshot(pid, _scraped(price=99.0, pdf_url="https://x/a.pdf"), "h1")
    db.log_alert(pid, sid, "price_change", 200)
    rows = db.get_all_latest_snapshots()
    assert len(rows) == 1
    r = rows[0]
    assert r["price"] == 99.0 and r["warranty_years"] == 5 and r["key_features"] == ["x"]
    assert r["last_alert_sent_at"] is not None


def test_failed_scrape_stored(db_file):
    pid = db.upsert_product("s1", "https://x/p1", "Widget")
    db.save_snapshot(pid, _scraped(price=None, stock=StockStatus.UNKNOWN, ok=False), None)
    last = db.get_last_snapshot(pid)
    assert last["price"] is None and last["scrape_success"] == 0
    assert last["error_message"] == "boom"


def test_get_settings_loads_without_env_file(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    s = get_settings()
    assert s.DB_PATH == "./data/products.db"
    assert s.PRICE_CHANGE_THRESHOLD_PCT == 5.0 and s.PLAYWRIGHT_HEADLESS is True
