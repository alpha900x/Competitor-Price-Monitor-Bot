"""Pure HTML -> ScrapedProduct parsing. No network, no browser, no env access.

Field order: schema.org JSON-LD (explicit currency/availability) first, CSS selectors second.
"""
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from src.models import ScrapedProduct, StockStatus, TargetConfig
from src.site_parsers import SiteParser

log = logging.getLogger(__name__)

# Order matters: out_of_stock / backorder are checked before in_stock ("unavailable" contains "available").
_STOCK_KEYWORDS: tuple[tuple[StockStatus, tuple[str, ...]], ...] = (
    (StockStatus.OUT_OF_STOCK, ("outofstock", "soldout", "unavailable", "notavailable", "nostock",
                                "discontinued", "notinstock")),
    (StockStatus.BACKORDER, ("backorder", "preorder", "shipsin", "shipswithin", "leadtime")),
    (StockStatus.IN_STOCK, ("instock", "available", "readytoship", "shipsimmediately")),
)
# "US$" must precede "$".
_CURRENCY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("US$", "USD"), ("₹", "INR"), ("Rs.", "INR"), ("Rs", "INR"), ("€", "EUR"), ("£", "GBP"), ("$", "USD"),
)


def parse_price(text: str | None) -> float | None:
    """'$2,522.00' -> 2522.0, '₹9,51,033' -> 951033.0, '12,50' -> 12.5. None if no positive number."""
    if not text:
        return None
    m = re.search(r"\d[\d,.]*", text)
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"\d{1,3}(?:,\d{2,3})*,\d{3}", s):  # thousands (western or Indian grouping)
            s = s.replace(",", "")
        elif re.fullmatch(r"\d+,\d{1,2}", s):  # decimal comma
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return value if value > 0 else None


def detect_currency(text: str | None) -> str | None:
    if not text:
        return None
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in text:
            return code
    return None


def normalize_stock(text: str | None) -> StockStatus:
    """Map arbitrary site text or a schema.org availability URL to StockStatus; no match -> UNKNOWN."""
    if not text:
        return StockStatus.UNKNOWN
    squashed = re.sub(r"[^a-z]", "", text.lower())
    for status, keywords in _STOCK_KEYWORDS:
        if any(k in squashed for k in keywords):
            return status
    return StockStatus.UNKNOWN


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _node_value(node) -> str:
    if node.tag == "meta":
        return _clean(node.attributes.get("content"))
    return _clean(node.text(separator=" ", strip=True))


def _json_ld_product(tree: LexborHTMLParser) -> dict | None:
    """First schema.org Product block that carries offers (falls back to first Product)."""
    products: list[dict] = []

    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            t = obj.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                products.append(obj)
            walk(obj.get("@graph", []))

    for script in tree.css('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.text(), strict=False))
        except (ValueError, TypeError):
            log.debug("Skipping unparseable JSON-LD block")
    return next((p for p in products if p.get("offers")), products[0] if products else None)


def _json_ld_offer(product: dict | None) -> dict:
    offers = (product or {}).get("offers")
    if isinstance(offers, list):
        offers = next((o for o in offers if isinstance(o, dict) and (o.get("price") or o.get("lowPrice"))), None)
    return offers if isinstance(offers, dict) else {}


def _first_css(tree: LexborHTMLParser, selectors: tuple[str, ...], accept=lambda v: bool(v)) -> str | None:
    for selector in selectors:
        for node in tree.css(selector):
            value = _node_value(node)
            if accept(value):
                return value
    return None


def _find_pdf(tree: LexborHTMLParser, parser: SiteParser, target: TargetConfig) -> str | None:
    """Pick a spec-sheet link; prefer one whose href/text mentions the product slug from the URL."""
    slug = target.product_url.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    candidates: list[str] = []
    for selector in parser.pdf_link_selectors:
        for node in tree.css(selector):
            href = (node.attributes.get("href") or "").strip()
            if href and not href.startswith(("#", "javascript:")):
                candidates.append((urljoin(target.product_url, href), _clean(node.text()).lower()))
        if candidates:
            break
    if not candidates:
        return None
    if slug:
        for href, text in candidates:
            if slug in href.lower() or slug in text:
                return href
    return candidates[0][0]


def parse_product_html(html: str, parser: SiteParser, target: TargetConfig,
                       scraped_at: datetime | None = None) -> ScrapedProduct:
    """Build a ScrapedProduct from page HTML. success=True only when a price was found."""
    tree = LexborHTMLParser(html)
    product = _json_ld_product(tree) if parser.use_json_ld else None
    offer = _json_ld_offer(product)

    title = _clean((product or {}).get("name")) or _first_css(tree, parser.title_selectors) or ""

    price, currency = None, None
    ld_price = offer.get("price", offer.get("lowPrice"))
    if ld_price not in (None, ""):
        price = parse_price(str(ld_price))
        currency = _clean(offer.get("priceCurrency")).upper() or None
    if price is None:
        price_text = _first_css(tree, parser.price_selectors, accept=lambda v: parse_price(v) is not None)
        price = parse_price(price_text)
        currency = detect_currency(price_text)
    currency = currency or parser.default_currency

    stock = normalize_stock(str(offer.get("availability") or ""))
    if stock is StockStatus.UNKNOWN:
        for selector in parser.stock_selectors:
            for node in tree.css(selector):
                stock = normalize_stock(_node_value(node))
                if stock is not StockStatus.UNKNOWN:
                    break
            if stock is not StockStatus.UNKNOWN:
                break

    success = price is not None
    return ScrapedProduct(
        site_id=target.site_id, product_url=target.product_url, title=title,
        price=price, currency=currency, stock_status=stock, pdf_url=_find_pdf(tree, parser, target),
        scraped_at=scraped_at or datetime.now(timezone.utc), success=success,
        error_message=None if success else "Price not found on page (selector/JSON-LD miss, or page not rendered)",
    )
