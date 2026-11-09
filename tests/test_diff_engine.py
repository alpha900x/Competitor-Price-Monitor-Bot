from datetime import datetime, timezone

import pytest

from src.diff_engine import alert_type, compute_diff
from src.models import DiffResult, ScrapedProduct, StockStatus

S = StockStatus


def cur(price=100.0, stock=S.IN_STOCK, success=True, currency="USD"):
    return ScrapedProduct(
        site_id="s", product_url="https://x/p", title="T", price=price, currency=currency,
        stock_status=stock, scraped_at=datetime.now(timezone.utc), success=success,
        error_message=None if success else "boom",
    )


def prev(price=100.0, stock="in_stock", success=1, currency="USD"):
    return {"price": price, "stock_status": stock, "scrape_success": success, "currency": currency}


# (id, current, previous, price_changed, stock_changed, should_alert, delta_pct)
CASES = [
    ("price_up_6pct", cur(106.0), prev(), True, False, True, 6.0),
    ("price_down_6pct", cur(94.0), prev(), True, False, True, -6.0),
    ("exactly_at_threshold", cur(105.0), prev(), True, False, True, 5.0),
    ("just_below_threshold", cur(104.9), prev(), False, False, False, 4.9),
    ("no_change", cur(100.0), prev(), False, False, False, 0.0),
    ("stock_change_only", cur(100.0, S.OUT_OF_STOCK), prev(), False, True, True, 0.0),
    ("price_and_stock", cur(110.0, S.BACKORDER), prev(), True, True, True, 10.0),
    ("first_run_no_previous", cur(106.0), None, False, False, False, None),
    ("failed_scrape", cur(None, S.UNKNOWN, success=False), prev(), False, False, False, None),
    ("current_price_none", cur(None), prev(), False, False, False, None),
    ("previous_scrape_failed", cur(200.0), prev(None, "unknown", success=0), False, False, False, None),
    ("previous_price_none", cur(200.0), prev(None), False, False, False, None),
    ("previous_price_zero", cur(200.0), prev(0.0), False, False, False, None),
    ("currency_changed", cur(8000.0, currency="INR"), prev(100.0), False, False, False, None),
    ("stock_unknown_now", cur(100.0, S.UNKNOWN), prev(), False, False, False, 0.0),
    ("stock_unknown_before", cur(100.0, S.IN_STOCK), prev(stock="unknown"), False, False, False, 0.0),
]


@pytest.mark.parametrize("name,current,previous,pc,sc,alert,delta", CASES, ids=[c[0] for c in CASES])
def test_compute_diff_table(name, current, previous, pc, sc, alert, delta):
    d = compute_diff(7, current, previous, 5.0)
    assert d.product_id == 7
    assert d.price_changed is pc
    assert d.stock_changed is sc
    assert d.should_alert is alert
    if delta is None:
        assert d.price_delta_pct is None
    else:
        assert d.price_delta_pct == pytest.approx(delta, abs=1e-6)


def test_stock_fields_populated_when_comparable():
    d = compute_diff(1, cur(100.0, S.OUT_OF_STOCK), prev(), 5.0)
    assert (d.old_stock, d.new_stock) == ("in_stock", "out_of_stock")


def test_stock_fields_none_on_first_run_and_failure():
    for current, previous in [(cur(), None), (cur(None, S.UNKNOWN, success=False), prev())]:
        d = compute_diff(1, current, previous, 5.0)
        assert d.old_stock is None and d.new_stock is None


def test_threshold_is_respected():
    assert compute_diff(1, cur(103.0), prev(), 2.0).should_alert is True
    assert compute_diff(1, cur(103.0), prev(), 5.0).should_alert is False


def test_same_day_rerun_does_not_realert():
    # second run compares against the snapshot saved by the first run (already at the new price)
    first = compute_diff(1, cur(106.0), prev(100.0), 5.0)
    assert first.should_alert is True
    second = compute_diff(1, cur(106.0), prev(106.0), 5.0)
    assert second.should_alert is False


@pytest.mark.parametrize(
    "pc,sc,expected",
    [(True, True, "both"), (True, False, "price_change"), (False, True, "stock_change"), (False, False, None)],
)
def test_alert_type(pc, sc, expected):
    d = DiffResult(product_id=1, price_changed=pc, stock_changed=sc, should_alert=pc or sc)
    assert alert_type(d) == expected
