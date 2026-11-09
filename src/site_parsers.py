"""Per-competitor extraction config. New competitor = add one entry to PARSERS.

Selectors were derived from saved page fixtures (tests/fixtures/), not guessed.
Extraction order per field (see parsing.py): schema.org JSON-LD first, then CSS selectors.
A selector pointing at a <meta> tag reads its `content` attribute.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SiteParser:
    site_id: str
    title_selectors: tuple[str, ...]
    price_selectors: tuple[str, ...]
    stock_selectors: tuple[str, ...]
    pdf_link_selectors: tuple[str, ...]
    price_wait_selector: str | None = None  # Playwright waits for this before reading the DOM
    default_currency: str = "USD"  # used only if neither JSON-LD nor the price text names one
    use_json_ld: bool = True


PARSERS: dict[str, SiteParser] = {
    # Lenovo India (SR250 V3). Prices are INR with Indian digit grouping (₹9,51,033).
    "lenovo_in": SiteParser(
        site_id="lenovo_in",
        title_selectors=("h1.product-name", 'meta[property="og:title"]'),
        price_selectors=("p.final-price",),
        stock_selectors=('meta[name="productstatus"]',),
        # "Datasheet" spec row links to a lenovopress.com HTML page (not a direct .pdf).
        pdf_link_selectors=('a[href*="lenovopress.com/ds"]',),
        price_wait_selector="p.final-price",
        default_currency="INR",
    ),
    # router-switch.com (Cisco C9300-24P-A). USD.
    "router_switch": SiteParser(
        site_id="router_switch",
        title_selectors=("h1", 'meta[property="og:title"]'),
        price_selectors=("div.saleprice span.regular-price", "span.regular-price"),
        stock_selectors=("div.product_availability_content",),
        pdf_link_selectors=('ul.download-list a[href*=".pdf"]',),
        price_wait_selector="span.regular-price",
        default_currency="USD",
    ),
}


class UnknownSiteError(KeyError):
    pass


def get_parser(site_id: str) -> SiteParser:
    try:
        return PARSERS[site_id]
    except KeyError:
        raise UnknownSiteError(f"No parser registered for site_id '{site_id}'") from None
