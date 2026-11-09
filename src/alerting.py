"""Slack Incoming Webhook alerts (Block Kit). Fails soft: never raises."""
from __future__ import annotations

import logging

import httpx

from src.config import get_settings
from src.models import DiffResult, ScrapedProduct, SpecSheetData

logger = logging.getLogger(__name__)

NOT_SENT = 0  # returned instead of an HTTP status when nothing was posted
_SYMBOLS = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}
_STOCK_LABELS = {
    "in_stock": "In stock",
    "out_of_stock": "Out of stock",
    "backorder": "Backorder",
    "unknown": "Unknown",
}
_MAX_FEATURES = 5


def format_price(amount: float | None, currency: str | None) -> str:
    """'$2,522.00', '₹9,51,033.00' (Indian grouping for INR), 'CHF 10.00' for unknown codes."""
    if amount is None:
        return "n/a"
    cur = (currency or "").upper()
    whole, _, frac = f"{abs(amount):.2f}".partition(".")
    if cur == "INR" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    else:
        whole = f"{int(whole):,}"
    body = f"{'-' if amount < 0 else ''}{whole}.{frac}"
    symbol = _SYMBOLS.get(cur)
    if symbol:
        return f"{symbol}{body}"
    return f"{cur} {body}".strip()


def _esc(text: str) -> str:
    """Escape Slack mrkdwn control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _stock_label(value: str | None) -> str:
    return _STOCK_LABELS.get(value or "", value or "n/a")


def _price_line(diff: DiffResult, product: ScrapedProduct) -> str:
    new = product.price
    pct = diff.price_delta_pct
    if diff.price_changed and new is not None and pct is not None:
        old = new / (1 + pct / 100.0)
        arrow = ":chart_with_upwards_trend:" if pct > 0 else ":chart_with_downwards_trend:"
        return f"{arrow} {format_price(old, product.currency)} → {format_price(new, product.currency)} ({pct:+.1f}%)"
    if pct is not None:
        return f"{format_price(new, product.currency)} ({pct:+.1f}%, below alert threshold)"
    return format_price(new, product.currency)


def _stock_line(diff: DiffResult, product: ScrapedProduct) -> str:
    if diff.stock_changed:
        return f":package: {_stock_label(diff.old_stock)} → {_stock_label(diff.new_stock)}"
    return f"{_stock_label(diff.new_stock or product.stock_status.value)} (unchanged)"


def _spec_lines(spec: SpecSheetData | None) -> list[str]:
    if spec is None:
        return []
    lines: list[str] = []
    if spec.warranty_years is not None:
        lines.append(f"*Warranty:* {spec.warranty_years} year(s)")
    if spec.power_consumption_watts is not None:
        lines.append(f"*Power:* {spec.power_consumption_watts} W")
    feats = [f for f in spec.key_features if f and f.strip()][:_MAX_FEATURES]
    if feats:
        lines.append("*Key features:*\n" + "\n".join(f"• {_esc(f.strip())}" for f in feats))
    return lines


def build_slack_payload(diff: DiffResult, product: ScrapedProduct, spec: SpecSheetData | None) -> dict:
    """Pure: Block Kit payload (JSON-safe dict) for one alert."""
    if diff.price_changed and diff.stock_changed:
        headline = "Price & stock change"
    elif diff.price_changed:
        headline = "Price change"
    else:
        headline = "Stock change"
    title = product.title or product.product_url
    fallback = f"{headline}: {title} ({product.site_id})"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:150], "emoji": True}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{product.product_url}|{_esc(title)[:200]}>*\nCompetitor: `{product.site_id}`",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Price*\n{_price_line(diff, product)}"},
                {"type": "mrkdwn", "text": f"*Stock*\n{_stock_line(diff, product)}"},
            ],
        },
    ]
    spec_lines = _spec_lines(spec)
    if spec_lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(spec_lines)[:2900]}})
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Scraped at {product.scraped_at.isoformat()}"}]}
    )
    return {"text": fallback, "blocks": blocks}


def send_slack_alert(
    diff: DiffResult,
    product: ScrapedProduct,
    spec: SpecSheetData | None,
    webhook_url: str,
) -> int:
    """POST one alert. Returns the HTTP status code from Slack, or 0 (NOT_SENT) if nothing was
    delivered (empty webhook URL or network/serialisation error). Never raises.
    Callers should only db.log_alert when the result is 200."""
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL is empty; alert for %s not sent", product.product_url)
        return NOT_SENT
    try:
        payload = build_slack_payload(diff, product, spec)
        resp = httpx.post(webhook_url, json=payload, timeout=get_settings().SCRAPER_TIMEOUT_SECONDS)
    except Exception as exc:  # fail soft; log the type only (messages can contain the secret URL)
        logger.error("Slack alert for %s failed: %s", product.product_url, type(exc).__name__)
        return NOT_SENT
    if resp.status_code != 200:
        logger.error("Slack returned HTTP %s for %s: %s", resp.status_code, product.product_url, resp.text[:200])
    return resp.status_code
