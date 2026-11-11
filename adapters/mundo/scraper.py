from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time
from pathlib import Path

from playwright.async_api import APIRequestContext, BrowserContext, Page, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from adapters.mundo.transform import (
    extract_next_listing_url,
    extract_product_links,
    extract_root_category_links,
    product_identifier_from_url,
    parse_product_record,
)
from config import get_cache_root, get_log_root
from core.contracts import SupplierScrapeResult


# Mundo rejects verbose automation and Chromium user agents, but serves the same
# public catalogue to a neutral browser-compatible request header.
USER_AGENT = "Mozilla/5.0"
REQUEST_TIMEOUT_MS = 60_000


class ScraperError(RuntimeError):
    pass


class MundoAdapter(SupplierAdapter):
    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        self.raw_html_dir = self.output_dir / "raw_html"
        self.cache_dir = get_cache_root() / config.supplier_slug
        self.log_dir = get_log_root() / config.supplier_slug
        self.log_path = self.log_dir / "scrape.log"
        self.detail_concurrency = max(1, int(config.scrape_settings.get("concurrency", 4)))
        self.min_delay_seconds = float(config.scrape_settings.get("min_delay_seconds", 0.15))
        self.max_delay_seconds = float(config.scrape_settings.get("max_delay_seconds", 0.4))
        self.max_pages = int(config.scrape_settings.get("max_pages", 0) or 0)
        self.max_products = int(config.scrape_settings.get("max_products", 0) or 0)

    def setup_logger(self) -> logging.Logger:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"supplier_scraper.{self.config.supplier_slug}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        for handler in (logging.FileHandler(self.log_path, encoding="utf-8"), logging.StreamHandler()):
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    async def scrape(self, *, force_refresh: bool = False) -> SupplierScrapeResult:
        logger = self.setup_logger()
        failures: list[dict[str, str]] = []
        listing_diagnostics: list[dict[str, str]] = []
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            browser_context = await browser.new_context(user_agent=USER_AGENT, locale="de-CH")
            request_context = await playwright.request.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={"Accept-Language": "de-CH,de;q=0.9"},
            )
            try:
                product_urls = await self._discover_product_urls(
                    browser_context, failures, listing_diagnostics, logger
                )
                if self.max_products:
                    product_urls = sorted(product_urls)[: self.max_products]
                products, raw_count, interpreted_count = await self._fetch_products(
                    request_context, product_urls, failures, logger, force_refresh=force_refresh
                )
            finally:
                await request_context.dispose()
                await browser_context.close()
                await browser.close()

        if not product_urls:
            failures.append(
                {
                    "stage": "discovery",
                    "url": self.base_url,
                    "reason": "Mundo returned no public product URLs from its catalogue categories",
                }
            )
        logger.info("Mundo scrape completed. discovered=%s parsed=%s failures=%s", len(product_urls), len(products), len(failures))
        return SupplierScrapeResult(
            products=products,
            failures=failures,
            discovered_product_urls=set(product_urls),
            listing_diagnostics=listing_diagnostics,
            # Only detail attempts count towards URL coverage. Listing failures do not
            # represent a discovered product URL and must not inflate this metric.
            covered_product_url_count=len(products) + sum(
                failure.get("stage") == "detail" for failure in failures
            ),
            raw_record_count=raw_count,
            interpreted_record_count=interpreted_count,
        )

    async def _discover_product_urls(
        self,
        browser_context: BrowserContext,
        failures: list[dict[str, str]],
        listing_diagnostics: list[dict[str, str]],
        logger: logging.Logger,
    ) -> list[str]:
        page = await browser_context.new_page()
        try:
            homepage = await self._load_listing(page, self.base_url)
            category_urls = extract_root_category_links(homepage, self.base_url)
            if not category_urls:
                raise ScraperError("No Mundo root catalogue categories found")
            seen_pages: set[str] = set()
            products: dict[str, str] = {}
            pending = list(category_urls)
            while pending:
                if self.max_pages and len(seen_pages) >= self.max_pages:
                    break
                page_url = pending.pop(0)
                if page_url in seen_pages:
                    continue
                seen_pages.add(page_url)
                try:
                    html = await self._load_listing(page, page_url)
                except Exception as exc:
                    failures.append({"stage": "listing", "url": page_url, "reason": str(exc)})
                    continue
                page_links = extract_product_links(html, self.base_url)
                for product_url in page_links:
                    product_id = product_identifier_from_url(product_url)
                    if product_id:
                        products.setdefault(product_id, product_url)
                next_url = extract_next_listing_url(html, self.base_url)
                if next_url and next_url not in seen_pages:
                    pending.append(next_url)
                listing_diagnostics.append(
                    {
                        "page_url": page_url,
                        "page_index": str(len(seen_pages)),
                        "product_url_count": str(len(page_links)),
                        "cumulative_product_url_count": str(len(products)),
                        "source": "public_catalogue_browser",
                    }
                )
                logger.info(
                    "PROGRESS phase=discovering found=%s pages=%s expected=%s",
                    len(products), len(seen_pages), 0,
                )
            return sorted(products.values())
        finally:
            await page.close()

    async def _load_listing(self, page: Page, url: str) -> str:
        await asyncio.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))
        response = await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT_MS)
        if response is None or not response.ok:
            status = response.status if response else "no response"
            raise ScraperError(f"HTTP {status} for Mundo listing {url}")
        try:
            await page.wait_for_selector('a[href*="/catalog2/products/"]', timeout=15_000)
        except Exception as exc:
            raise ScraperError(f"Mundo listing did not render products for {url}") from exc
        return await page.content()

    def _snapshot_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", url.split("://", 1)[-1])[:100]
        return self.raw_html_dir / f"{digest}_{slug}.html"

    async def _fetch_products(
        self,
        request_context: APIRequestContext,
        product_urls: list[str],
        failures: list[dict[str, str]],
        logger: logging.Logger,
        *,
        force_refresh: bool,
    ) -> tuple[list, int, int]:
        semaphore = asyncio.Semaphore(self.detail_concurrency)
        total = len(product_urls)
        processed = 0
        scraped = 0
        logger.info("PROGRESS phase=processing found=%s processed=0 scraped=0 total=%s", total, total)

        async def fetch_product(url: str):
            nonlocal processed, scraped
            product = None
            try:
                async with semaphore:
                    snapshot = self._snapshot_path(url)
                    if snapshot.exists() and not force_refresh:
                        html = snapshot.read_text(encoding="utf-8", errors="replace")
                    else:
                        async for attempt in AsyncRetrying(
                            stop=stop_after_attempt(4),
                            wait=wait_exponential(multiplier=1, min=1, max=10),
                            retry=retry_if_exception_type(Exception),
                            reraise=True,
                        ):
                            with attempt:
                                await asyncio.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))
                                response = await request_context.get(url, timeout=REQUEST_TIMEOUT_MS)
                                if not response.ok:
                                    raise ScraperError(f"HTTP {response.status} for Mundo product {url}")
                                html = await response.text()
                                snapshot.write_text(html, encoding="utf-8")
                    product = parse_product_record(html, url)
                    if product is None:
                        raise ScraperError("Mundo detail page did not contain a usable product payload")
                    return product
            finally:
                processed += 1
                if product is not None:
                    scraped += 1
                logger.info(
                    "PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s",
                    total, processed, scraped, total,
                )

        results = await asyncio.gather(*(fetch_product(url) for url in product_urls), return_exceptions=True)
        products = []
        raw_count = 0
        for url, result in zip(product_urls, results, strict=True):
            if isinstance(result, Exception):
                failures.append({"stage": "detail", "url": url, "reason": str(result)})
                continue
            raw_count += 1
            products.append(result)
        return products, raw_count, len(products)
