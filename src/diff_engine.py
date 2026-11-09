"""Compare today's scrape against the last stored snapshot and decide what is alert-worthy.

Pure functions: no I/O, no DB access, no env reads. See PROJECT_STATE.md for the contract.

Rules (ROADMAP Sprint 3 + conservative guards, all fail-quiet):
- previous is None (first ever run)  -> baseline only, no alert.
- current scrape failed / no price    -> no price or stock alert (never a false positive).
- previous scrape failed / no price   -> no comparison against it.
- currency differs between snapshots  -> price not compared (sites have currency switchers).
- stock UNKNOWN on either side        -> not treated as a stock change (likely a parse gap).
"""
from __future__ import annotations

from src.models import DiffResult, ScrapedProduct, StockStatus

_EPS = 1e-9


def compute_diff(
    product_id: int,
    current: ScrapedProduct,
    previous: dict | None,
    threshold_pct: float,
) -> DiffResult:
    """Return a DiffResult. `previous` is the dict from db.get_last_snapshot (or None).

    price_changed  = price moved by >= threshold_pct (alert-worthy move, not any move).
    price_delta_pct = SIGNED % change (+ = increase), unrounded, set whenever comparable,
                      even when below the threshold.
    old_stock / new_stock are set whenever the stock comparison was valid, else None.
    """
    no_change = DiffResult(
        product_id=product_id,
        price_changed=False,
        price_delta_pct=None,
        stock_changed=False,
        old_stock=None,
        new_stock=None,
        should_alert=False,
    )

    if previous is None or not current.success:
        return no_change
    if not previous.get("scrape_success", 1):
        return no_change

    # --- price ---
    price_changed = False
    delta_pct: float | None = None
    old_price = previous.get("price")
    new_price = current.price
    same_currency = (previous.get("currency") or current.currency) == current.currency
    if new_price is not None and old_price and old_price > 0 and same_currency:
        delta_pct = (new_price - old_price) * 100.0 / old_price
        price_changed = abs(delta_pct) >= threshold_pct - _EPS

    # --- stock ---
    old_stock = previous.get("stock_status")
    new_stock = current.stock_status.value
    stock_changed = (
        old_stock is not None
        and old_stock != StockStatus.UNKNOWN.value
        and new_stock != StockStatus.UNKNOWN.value
        and old_stock != new_stock
    )

    return DiffResult(
        product_id=product_id,
        price_changed=price_changed,
        price_delta_pct=delta_pct,
        stock_changed=stock_changed,
        old_stock=old_stock,
        new_stock=new_stock,
        should_alert=price_changed or stock_changed,
    )


def alert_type(diff: DiffResult) -> str | None:
    """Value for db.log_alert(alert_type=...): 'price_change' | 'stock_change' | 'both' | None."""
    if diff.price_changed and diff.stock_changed:
        return "both"
    if diff.price_changed:
        return "price_change"
    if diff.stock_changed:
        return "stock_change"
    return None
