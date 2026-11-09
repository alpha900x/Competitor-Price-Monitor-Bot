"""Daily orchestrator: scrape -> diff -> spec -> snapshot -> alert -> CSV.

Run from anywhere: `python src/main.py` or `python -m src.main`.
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):  # `python src/main.py`: make `from src.x import` work
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import logging

import httpx
from pydantic import BaseModel

from src import alerting, db, diff_engine, reporting, scraper, spec_pipeline
from src.config import Settings, get_settings
from src.logging_setup import setup_logging
from src.models import ScrapedProduct, TargetConfig

log = logging.getLogger(__name__)

CSV_NAME = "summary_report.csv"


class RunSummary(BaseModel):
    products_checked: int = 0
    alerts_sent: int = 0
    errors: list[str] = []


def load_targets(path: str) -> list[TargetConfig]:
    """Read targets.json. Raises on a missing/invalid file (fatal for the run)."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return [TargetConfig(**item) for item in raw]


def _process_product(scraped: ScrapedProduct, settings: Settings,
                     summary: RunSummary, state: dict) -> None:
    """One product. Caller isolates exceptions; this never touches other products."""
    title = scraped.title
    if not title:  # failed scrape has title=""; keep the stored title
        title = state["titles"].get(scraped.product_url, "")
    product_id = db.upsert_product(scraped.site_id, scraped.product_url, title)

    previous = db.get_last_snapshot(product_id)  # BEFORE saving the current snapshot
    diff = diff_engine.compute_diff(
        product_id, scraped, previous, settings.PRICE_CHANGE_THRESHOLD_PCT)

    spec = None
    pdf_hash = None
    if scraped.success and scraped.pdf_url:
        result = spec_pipeline.get_or_extract_spec(product_id, scraped.pdf_url, settings)
        if result.error:
            summary.errors.append(f"{scraped.site_id}: spec: {result.error}")
        if result.spec is not None:  # only store the hash when a spec row exists
            spec, pdf_hash = result.spec, result.pdf_hash

    snapshot_id = db.save_snapshot(product_id, scraped, pdf_hash)
    if not scraped.success:
        summary.errors.append(f"{scraped.site_id}: scrape failed: {scraped.error_message}")

    if not diff.should_alert:
        return
    if not settings.SLACK_WEBHOOK_URL:
        if not state["webhook_warned"]:
            state["webhook_warned"] = True
            summary.errors.append("SLACK_WEBHOOK_URL is empty: alerts not sent")
        return
    status = alerting.send_slack_alert(diff, scraped, spec, settings.SLACK_WEBHOOK_URL)
    if status == 200:
        db.log_alert(product_id, snapshot_id, diff_engine.alert_type(diff), status)
        summary.alerts_sent += 1
    else:
        summary.errors.append(f"{scraped.site_id}: Slack alert failed (status {status})")


def _notify_run_failed(settings: Settings, message: str) -> None:
    """Best-effort plain-text Slack message; never raises, never logs the URL."""
    if not settings.SLACK_WEBHOOK_URL:
        return
    try:
        httpx.post(settings.SLACK_WEBHOOK_URL,
                   json={"text": f":rotating_light: Price monitor run failed: {message}"},
                   timeout=settings.SCRAPER_TIMEOUT_SECONDS)
    except Exception as exc:  # message could embed the URL: log type only
        log.error("run-failed notification not sent (%s)", type(exc).__name__)


def run_daily_job() -> RunSummary:
    summary = RunSummary()
    settings = get_settings()
    setup_logging(settings.LOG_FILE_PATH)
    try:
        db.init_db(settings.DB_PATH)
        targets = load_targets(settings.TARGETS_CONFIG_PATH)
        scraped_all = scraper.fetch_all(targets)
        known = {r["product_url"]: r["product_title"] for r in db.get_all_latest_snapshots()}
        state = {"titles": known, "webhook_warned": False}
        for item in scraped_all:
            summary.products_checked += 1
            try:
                _process_product(item, settings, summary, state)
            except Exception as exc:  # fail soft per product
                log.exception("product failed: %s", item.product_url)
                summary.errors.append(f"{item.site_id}: {type(exc).__name__}: {exc}")
        reporting.export_csv(os.path.join(settings.REPORT_OUTPUT_DIR, CSV_NAME))
    except Exception as exc:
        log.exception("run failed")
        summary.errors.append(f"run failed: {type(exc).__name__}: {exc}")
        _notify_run_failed(settings, type(exc).__name__)
    log.info("run done: checked=%d alerts=%d errors=%d",
             summary.products_checked, summary.alerts_sent, len(summary.errors))
    return summary


if __name__ == "__main__":
    result = run_daily_job()
    print(result.model_dump_json(indent=2))
    sys.exit(1 if any(e.startswith("run failed") for e in result.errors) else 0)
