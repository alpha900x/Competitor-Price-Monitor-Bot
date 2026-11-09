"""Fetching layer: static (httpx) and dynamic (Playwright sync) paths, retries, fail-soft entrypoints.

Parsing lives in parsing.py. Retries: SCRAPER_MAX_RETRIES = extra attempts after the first
(so 2 -> up to 3 attempts), exponential backoff 1s, 2s, 4s... capped at 8s. Only transient
errors (network, timeout, HTTP 429/5xx) are retried. HTTP 4xx (incl. 403) fails immediately:
bot-blocking is logged and reported, never evaded (out of scope).
"""
import logging
import time
from datetime import datetime, timezone
from typing import Callable, TypeVar

import httpx

from src.config import Settings, get_settings
from src.models import ScrapedProduct, StockStatus, TargetConfig
from src.parsing import parse_product_html
from src.site_parsers import SiteParser, get_parser

log = logging.getLogger(__name__)
T = TypeVar("T")
_BLOCKED_RESOURCES = {"image", "media", "font"}


class TransientScrapeError(Exception):
    """Worth retrying (network hiccup, timeout, 429/5xx)."""


class PermanentScrapeError(Exception):
    """Not worth retrying (4xx, bad config)."""


def _with_retries(fn: Callable[[], T], max_retries: int, sleep: Callable[[float], None] = time.sleep) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except TransientScrapeError as exc:
            if attempt >= max_retries:
                raise
            delay = min(2 ** attempt, 8)
            log.warning("Transient error (%s); retry %d/%d in %ss", exc, attempt + 1, max_retries, delay)
            sleep(delay)
            attempt += 1


def _check_status(status: int, url: str) -> None:
    if status == 429 or status >= 500:
        raise TransientScrapeError(f"HTTP {status} from {url}")
    if status >= 400:
        hint = " (possible bot blocking; not evading, flag to client)" if status in (401, 403) else ""
        raise PermanentScrapeError(f"HTTP {status} from {url}{hint}")


def _fetch_static_html(url: str, settings: Settings) -> str:
    try:
        resp = httpx.get(url, headers={"User-Agent": settings.SCRAPER_USER_AGENT, "Accept-Language": "en"},
                         timeout=settings.SCRAPER_TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.TransportError as exc:  # timeouts, connection resets, DNS
        raise TransientScrapeError(f"{type(exc).__name__}: {exc}") from exc
    _check_status(resp.status_code, url)
    return resp.text


def _fetch_dynamic_html(url: str, parser: SiteParser, settings: Settings,
                        page_setup: Callable | None = None) -> str:
    """Render with headless chromium, wait for the price element, return the DOM as HTML.

    `page_setup(page)` is an optional hook (used by tests to serve fixtures); production passes None.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    timeout_ms = int(settings.SCRAPER_TIMEOUT_SECONDS * 1000)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.PLAYWRIGHT_HEADLESS)
            try:
                context = browser.new_context(user_agent=settings.SCRAPER_USER_AGENT)
                page = context.new_page()
                page.route("**/*", lambda route: route.abort()
                           if route.request.resource_type in _BLOCKED_RESOURCES else route.continue_())
                if page_setup:
                    page_setup(page)
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if response is not None:
                    _check_status(response.status, url)
                if parser.price_wait_selector:
                    try:
                        page.wait_for_selector(parser.price_wait_selector, timeout=timeout_ms)
                    except PlaywrightTimeout:
                        # Still return the DOM: JSON-LD may carry the price; parsing decides success.
                        log.warning("Price selector '%s' not found within %sms on %s",
                                    parser.price_wait_selector, timeout_ms, url)
                return page.content()
            finally:
                browser.close()
    except PlaywrightTimeout as exc:
        raise TransientScrapeError(f"Playwright timeout: {exc}") from exc
    except PlaywrightError as exc:
        raise TransientScrapeError(f"Playwright error: {exc}") from exc


def _failed(target: TargetConfig, message: str) -> ScrapedProduct:
    # title="" on purpose: Sprint 4 must not overwrite a stored product title with an empty one.
    return ScrapedProduct(site_id=target.site_id, product_url=target.product_url, title="", price=None,
                          stock_status=StockStatus.UNKNOWN, scraped_at=datetime.now(timezone.utc),
                          success=False, error_message=message)


def fetch_product(target: TargetConfig) -> ScrapedProduct:
    """Scrape one product. Never raises: failures come back as success=False + error_message."""
    try:
        settings = get_settings()
        parser = get_parser(target.site_id)

        def attempt() -> str:
            if target.requires_js:
                return _fetch_dynamic_html(target.product_url, parser, settings)
            return _fetch_static_html(target.product_url, settings)

        html = _with_retries(attempt, settings.SCRAPER_MAX_RETRIES)
        result = parse_product_html(html, parser, target)
        if not result.success:
            log.warning("%s: %s", target.product_url, result.error_message)
        return result
    except Exception as exc:  # fail soft by contract
        log.exception("Scrape failed for %s", target.product_url)
        return _failed(target, f"{type(exc).__name__}: {exc}")


def fetch_all(targets: list[TargetConfig]) -> list[ScrapedProduct]:
    """Scrape every target; one failing site never stops the others."""
    results: list[ScrapedProduct] = []
    for target in targets:
        try:
            results.append(fetch_product(target))
        except Exception as exc:  # belt and braces; fetch_product should never raise
            log.exception("Unexpected error for %s", target.product_url)
            results.append(_failed(target, f"{type(exc).__name__}: {exc}"))
    ok = sum(r.success for r in results)
    log.info("fetch_all: %d/%d targets scraped successfully", ok, len(results))
    return results
