"""CSV export of the latest state per product."""
from __future__ import annotations

import csv
import logging
import os
import tempfile

from src import db

logger = logging.getLogger(__name__)

COLUMNS = [
    "site_id",
    "product_title",
    "price",
    "currency",
    "stock_status",
    "warranty_years",
    "power_consumption_watts",
    "key_features",
    "last_scraped_at",
    "last_alert_sent_at",
    # Added beyond the ROADMAP list: ARCHITECTURE §1 requires failures to appear in the report.
    "scrape_success",
    "error_message",
]
_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _text(value: object) -> str:
    """None -> ''. Text starting with a spreadsheet formula character is prefixed with ' (CSV injection guard)."""
    if value is None:
        return ""
    s = str(value)
    return "'" + s if s.startswith(_FORMULA_CHARS) else s


def _row(snap: dict) -> dict:
    feats = snap.get("key_features") or []
    return {
        "site_id": _text(snap.get("site_id")),
        "product_title": _text(snap.get("product_title")),
        "price": "" if snap.get("price") is None else snap["price"],
        "currency": _text(snap.get("currency")),
        "stock_status": _text(snap.get("stock_status")),
        "warranty_years": "" if snap.get("warranty_years") is None else snap["warranty_years"],
        "power_consumption_watts": (
            "" if snap.get("power_consumption_watts") is None else snap["power_consumption_watts"]
        ),
        "key_features": _text(" | ".join(str(f) for f in feats)),
        "last_scraped_at": _text(snap.get("last_scraped_at")),
        "last_alert_sent_at": _text(snap.get("last_alert_sent_at")),
        "scrape_success": "" if snap.get("scrape_success") is None else int(bool(snap["scrape_success"])),
        "error_message": _text(snap.get("error_message")),
    }


def export_csv(output_path: str) -> str:
    """Write one row per product (latest snapshot) to `output_path`; returns the path written.
    UTF-8 with BOM so Excel opens it correctly. Written atomically (temp file + rename)."""
    rows = [_row(s) for s in db.get_all_latest_snapshots()]
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, output_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    logger.info("CSV report written: %s (%d rows)", output_path, len(rows))
    return output_path
