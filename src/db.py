"""SQLite persistence (see ARCHITECTURE.md section 5).

Signatures other than init_db() carry no db path, so init_db() records the
active path in a module-level variable. If it was never called, the path
falls back to get_settings().DB_PATH.
"""
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from src.config import get_settings
from src.models import ScrapedProduct, SpecSheetData

logger = logging.getLogger(__name__)

_db_path: str | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL,
    product_url TEXT NOT NULL UNIQUE,
    product_title TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    scraped_at TEXT NOT NULL,
    price REAL,
    currency TEXT DEFAULT 'USD',
    stock_status TEXT,
    pdf_url TEXT,
    pdf_hash TEXT,
    scrape_success INTEGER NOT NULL,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS spec_sheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    pdf_url TEXT,
    pdf_hash TEXT NOT NULL UNIQUE,
    warranty_years INTEGER,
    power_consumption_watts INTEGER,
    key_features TEXT,
    extracted_at TEXT NOT NULL,
    raw_ai_response TEXT
);
CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    alert_type TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    slack_response_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snapshots_product ON snapshots(product_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> str:
    return _db_path or get_settings().DB_PATH


@contextmanager
def _connect():
    conn = sqlite3.connect(_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Create the DB file (and parent dir) and all tables if missing."""
    global _db_path
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    _db_path = db_path
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    logger.info("Database initialised at %s", db_path)


def upsert_product(site_id: str, product_url: str, title: str) -> int:
    """Insert a product (keyed by URL) or update its latest title. Returns product_id."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, product_title FROM products WHERE product_url = ?", (product_url,)
        ).fetchone()
        if row:
            if title and title != row["product_title"]:
                conn.execute(
                    "UPDATE products SET product_title = ? WHERE id = ?", (title, row["id"])
                )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO products (site_id, product_url, product_title, created_at) "
            "VALUES (?, ?, ?, ?)",
            (site_id, product_url, title, _now()),
        )
        return int(cur.lastrowid)


def save_snapshot(product_id: int, scraped: ScrapedProduct, pdf_hash: str | None) -> int:
    """Store one scrape result. Returns snapshot_id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO snapshots (product_id, scraped_at, price, currency, stock_status, "
            "pdf_url, pdf_hash, scrape_success, error_message) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                product_id,
                scraped.scraped_at.isoformat(),
                scraped.price,
                scraped.currency,
                scraped.stock_status.value,
                scraped.pdf_url,
                pdf_hash,
                1 if scraped.success else 0,
                scraped.error_message,
            ),
        )
        return int(cur.lastrowid)


def get_last_snapshot(product_id: int) -> dict | None:
    """Most recent stored snapshot for the product (call before saving the current one)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
    return dict(row) if row else None


def save_spec_sheet(
    product_id: int, pdf_url: str, pdf_hash: str, spec: SpecSheetData, raw_response: str
) -> int:
    """Store extracted spec data keyed by unique pdf_hash. Returns spec_sheets.id.

    If the hash already exists, the existing row id is returned (no duplicate).
    """
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM spec_sheets WHERE pdf_hash = ?", (pdf_hash,)
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            "INSERT INTO spec_sheets (product_id, pdf_url, pdf_hash, warranty_years, "
            "power_consumption_watts, key_features, extracted_at, raw_ai_response) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                product_id,
                pdf_url,
                pdf_hash,
                spec.warranty_years,
                spec.power_consumption_watts,
                json.dumps(spec.key_features),
                _now(),
                raw_response,
            ),
        )
        return int(cur.lastrowid)


def get_spec_sheet_by_hash(pdf_hash: str) -> dict | None:
    """Return the stored spec row (key_features decoded to list) or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM spec_sheets WHERE pdf_hash = ?", (pdf_hash,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["key_features"] = json.loads(d["key_features"]) if d["key_features"] else []
    return d


def log_alert(product_id: int, snapshot_id: int, alert_type: str, slack_status_code: int) -> None:
    """Record a sent alert. alert_type: price_change / stock_change / both."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO alerts_log (product_id, snapshot_id, alert_type, sent_at, "
            "slack_response_code) VALUES (?,?,?,?,?)",
            (product_id, snapshot_id, alert_type, _now(), slack_status_code),
        )


def get_all_latest_snapshots() -> list[dict]:
    """One row per product: latest snapshot + latest spec (by pdf_hash) + last alert time.

    Keys: site_id, product_title, product_url, price, currency, stock_status,
    scrape_success, error_message, pdf_url, warranty_years,
    power_consumption_watts, key_features (list[str]), last_scraped_at,
    last_alert_sent_at.
    """
    sql = """
    SELECT p.id AS product_id, p.site_id, p.product_title, p.product_url,
           s.price, s.currency, s.stock_status, s.scrape_success, s.error_message,
           s.pdf_url, s.scraped_at AS last_scraped_at,
           sp.warranty_years, sp.power_consumption_watts, sp.key_features,
           (SELECT MAX(a.sent_at) FROM alerts_log a WHERE a.product_id = p.id)
               AS last_alert_sent_at
    FROM products p
    JOIN snapshots s ON s.id = (
        SELECT MAX(id) FROM snapshots WHERE product_id = p.id)
    LEFT JOIN spec_sheets sp ON sp.pdf_hash = s.pdf_hash
    ORDER BY p.site_id, p.id
    """
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["key_features"] = json.loads(d["key_features"]) if d["key_features"] else []
        out.append(d)
    return out
