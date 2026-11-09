import re
from pathlib import Path

import httpx
import pytest

from src import scraper
from src.models import StockStatus, TargetConfig
from src.parsing import normalize_stock, parse_price, parse_product_html
from src.site_parsers import PARSERS, SiteParser, UnknownSiteError, get_parser

FX = Path(__file__).parent / "fixtures"
LENOVO_URL = ("https://www.lenovo.com/in/en/p/servers-storage/servers/racks/"
              "thinksystem-sr250-v3-rack-server/len21ts0024")
ROUTER_URL = "https://www.router-switch.com/c9300-24p-a.html"
LENOVO = TargetConfig(site_id="lenovo_in", product_url=LENOVO_URL, requires_js=True)
ROUTER = TargetConfig(site_id="router_switch", product_url=ROUTER_URL, requires_js=False)


def html(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8", errors="replace")


def strip_json_ld(s: str) -> str:
    return re.sub(r'<script type="application/ld\+json">.*?</script>', "", s, flags=re.S)


# ---------- helpers ----------
@pytest.mark.parametrize("text,expected", [
    ("$2,522.00", 2522.0), ("US$2,522.00", 2522.0), ("₹9,51,033", 951033.0),
    ("Starting at₹9,51,033", 951033.0), ("1,234,567.89", 1234567.89), ("12,50", 12.5),
    ("1.234,56", 1234.56), ("2522", 2522.0), ("Call for price", None), ("", None), (None, None), ("$0.00", None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("In Stock", StockStatus.IN_STOCK), ("Available", StockStatus.IN_STOCK),
    ("https://schema.org/InStock", StockStatus.IN_STOCK),
    ("http://schema.org/OutOfStock", StockStatus.OUT_OF_STOCK), ("Sold out", StockStatus.OUT_OF_STOCK),
    ("Currently unavailable", StockStatus.OUT_OF_STOCK), ("Not in stock", StockStatus.OUT_OF_STOCK),
    ("Back-order", StockStatus.BACKORDER), ("https://schema.org/PreOrder", StockStatus.BACKORDER),
    ("Ships in 2 days", StockStatus.BACKORDER), ("Quantity: 5", StockStatus.UNKNOWN),
    ("", StockStatus.UNKNOWN), (None, StockStatus.UNKNOWN),
])
def test_normalize_stock(text, expected):
    assert normalize_stock(text) == expected


# ---------- parsing real saved pages ----------
def test_parse_lenovo_fixture():
    p = parse_product_html(html("lenovo_in.html"), get_parser("lenovo_in"), LENOVO)
    assert (p.success, p.title, p.price, p.currency) == (True, "ThinkSystem SR250 V3 Rack Server", 951033.0, "INR")
    assert p.stock_status == StockStatus.IN_STOCK
    assert p.pdf_url == "http://lenovopress.com/ds0171"


def test_parse_router_fixture():
    p = parse_product_html(html("router_switch.html"), get_parser("router_switch"), ROUTER)
    assert (p.success, p.price, p.currency) == (True, 2522.0, "USD")
    assert p.title.startswith("C9300-24P-A, Cisco Catalyst 9300")
    assert p.stock_status == StockStatus.IN_STOCK
    assert p.pdf_url == "https://www.router-switch.com/pdf/c9300-24p-a-datasheet.pdf"  # model sheet, not generic one


@pytest.mark.parametrize("name,target", [("lenovo_in.html", LENOVO), ("router_switch.html", ROUTER)])
def test_css_selectors_work_without_json_ld(name, target):
    """Proves the CSS fallbacks independently of JSON-LD."""
    full = parse_product_html(html(name), get_parser(target.site_id), target)
    css = parse_product_html(strip_json_ld(html(name)), get_parser(target.site_id), target)
    assert (css.title, css.price, css.currency, css.stock_status, css.pdf_url) == \
           (full.title, full.price, full.currency, full.stock_status, full.pdf_url)


def test_missing_price_is_failure_not_exception():
    p = parse_product_html("<html><h1>x</h1></html>", get_parser("router_switch"), ROUTER)
    assert p.success is False and p.price is None and p.error_message


def test_unknown_site_raises_from_get_parser():
    with pytest.raises(UnknownSiteError):
        get_parser("nope")


# ---------- static path ----------
def test_fetch_product_static_path(monkeypatch):
    def fake_get(url, **kw):
        assert url == ROUTER_URL and "PriceMonitorBot" in kw["headers"]["User-Agent"]
        return httpx.Response(200, text=html("router_switch.html"), request=httpx.Request("GET", url))
    monkeypatch.setattr(scraper.httpx, "get", fake_get)
    p = scraper.fetch_product(ROUTER)
    assert p.success and p.price == 2522.0


def test_retry_transient_then_success(monkeypatch):
    calls, sleeps = [], []
    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise scraper.TransientScrapeError("boom")
        return "ok"
    assert scraper._with_retries(fn, 2, sleep=sleeps.append) == "ok"
    assert len(calls) == 3 and sleeps == [1, 2]


def test_retry_gives_up_after_max():
    with pytest.raises(scraper.TransientScrapeError):
        scraper._with_retries(lambda: (_ for _ in ()).throw(scraper.TransientScrapeError("x")), 1, sleep=lambda s: None)


def test_http_404_fails_soft_without_retry(monkeypatch):
    n = []
    def fake_get(url, **kw):
        n.append(1)
        return httpx.Response(404, request=httpx.Request("GET", url))
    monkeypatch.setattr(scraper.httpx, "get", fake_get)
    p = scraper.fetch_product(ROUTER)
    assert p.success is False and "404" in p.error_message and len(n) == 1


def test_http_403_flags_blocking_no_evasion(monkeypatch):
    monkeypatch.setattr(scraper.httpx, "get",
                        lambda url, **kw: httpx.Response(403, request=httpx.Request("GET", url)))
    p = scraper.fetch_product(ROUTER)
    assert p.success is False and "bot blocking" in p.error_message


def test_network_error_retried_then_fails_soft(monkeypatch):
    n = []
    def fake_get(url, **kw):
        n.append(1)
        raise httpx.ConnectError("down")
    monkeypatch.setattr(scraper.httpx, "get", fake_get)
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    p = scraper.fetch_product(ROUTER)
    assert p.success is False and "ConnectError" in p.error_message and len(n) == 3  # 1 + SCRAPER_MAX_RETRIES(2)


def test_fetch_all_isolates_failures(monkeypatch):
    def fake_get(url, **kw):
        if "router-switch" in url:
            return httpx.Response(200, text=html("router_switch.html"), request=httpx.Request("GET", url))
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(scraper.httpx, "get", fake_get)
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    bad = TargetConfig(site_id="lenovo_in", product_url=LENOVO_URL, requires_js=False)
    unknown = TargetConfig(site_id="ghost", product_url="https://x.test/p", requires_js=False)
    res = scraper.fetch_all([bad, ROUTER, unknown])
    assert [r.success for r in res] == [False, True, False]
    assert "ghost" in res[2].error_message


# ---------- dynamic (Playwright) path ----------
def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch().close()
        return True
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(not _chromium_ok(), reason="chromium not available")


@needs_chromium
def test_js_rendered_price_needs_playwright(monkeypatch):
    """Synthetic page: price is injected 700ms after load. Static parse misses it; Playwright path gets it."""
    parser = SiteParser(site_id="js_demo", title_selectors=("h1",), price_selectors=("span.js-price",),
                        stock_selectors=("#stock",), pdf_link_selectors=(), price_wait_selector="span.js-price")
    monkeypatch.setitem(PARSERS, "js_demo", parser)
    url = (FX / "js_price_demo.html").resolve().as_uri()
    target = TargetConfig(site_id="js_demo", product_url=url, requires_js=True)

    raw = (FX / "js_price_demo.html").read_text()
    assert parse_product_html(raw, parser, target).success is False  # static HTML has no price

    p = scraper.fetch_product(target)
    assert p.success and p.price == 1299.50 and p.title == "Demo Widget"
    assert p.stock_status == StockStatus.BACKORDER  # "Ships in 3 days"


@needs_chromium
@pytest.mark.parametrize("name,target", [("lenovo_in.html", LENOVO), ("router_switch.html", ROUTER)])
def test_playwright_path_on_real_fixture_pages(name, target):
    """Real Playwright render of the saved real pages, served offline for the real URL (all else aborted)."""
    body = html(name)
    def setup(page):
        page.unroute("**/*")
        def handler(route):
            if route.request.url == target.product_url:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
            else:
                route.abort()
        page.route("**/*", handler)
    from src.config import get_settings
    parser = get_parser(target.site_id)
    rendered = scraper._fetch_dynamic_html(target.product_url, parser, get_settings(), page_setup=setup)
    p = parse_product_html(rendered, parser, target)
    assert p.success and p.price in (951033.0, 2522.0) and p.stock_status == StockStatus.IN_STOCK and p.pdf_url
