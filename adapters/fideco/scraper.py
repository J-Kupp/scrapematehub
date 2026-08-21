from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path

from playwright.async_api import APIRequestContext, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from adapters.fideco.transform import (
    extract_category_links,
    extract_listing_page_count,
    extract_product_links,
    listing_page_url,
    parse_product_record,
    product_identifier_from_url,
)
from config import get_cache_root, get_log_root
from core.contracts import SupplierScrapeResult


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
REQUEST_TIMEOUT_MS = 60_000


class ScraperError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, context: APIRequestContext, *, raw_html_dir: Path, cache_dir: Path, min_delay_seconds: float, max_delay_seconds: float) -> None:
        self.context = context
        self.raw_html_dir = raw_html_dir
        self.cache_manifest = cache_dir / "manifest.jsonl"
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        cache_dir.mkdir(parents=True, exist_ok=True)

    def snapshot_path(self, url: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", url.split("://", 1)[-1])[:120]
        return self.raw_html_dir / f"{hashlib.sha1(url.encode()).hexdigest()[:12]}_{slug}.html"

    async def fetch_text(self, url: str, *, force_refresh: bool) -> str:
        snapshot_path = self.snapshot_path(url)
        if snapshot_path.exists() and not force_refresh:
            return snapshot_path.read_text(encoding="utf-8", errors="replace")
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        async for attempt in AsyncRetrying(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=10), retry=retry_if_exception_type(Exception), reraise=True):
            with attempt:
                await asyncio.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))
                response = await self.context.get(url, timeout=REQUEST_TIMEOUT_MS)
                if not response.ok:
                    raise ScraperError(f"HTTP {response.status} for {url}")
                html = await response.text()
                snapshot_path.write_text(html, encoding="utf-8")
                with self.cache_manifest.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "url": url, "status_code": response.status, "snapshot_path": str(snapshot_path)}) + "\n")
                return html
        raise ScraperError(f"Could not fetch {url}")


class FidecoAdapter(SupplierAdapter):
    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        # Fideco has no /Shop/ index. Its public homepage exposes the complete
        # product-group tree that drives discovery.
        self.listing_url = self.base_url
        self.raw_html_dir = self.output_dir / "raw_html"
        self.cache_dir = get_cache_root() / config.supplier_slug
        self.log_dir = get_log_root() / config.supplier_slug
        self.log_path = self.log_dir / "scrape.log"
        self.detail_concurrency = max(1, int(config.scrape_settings.get("concurrency", 8)))
        self.category_concurrency = max(1, int(config.scrape_settings.get("category_concurrency", 8)))
        self.min_delay_seconds = float(config.scrape_settings.get("min_delay_seconds", 0.05))
        self.max_delay_seconds = float(config.scrape_settings.get("max_delay_seconds", 0.15))
        self.max_pages = int(config.scrape_settings.get("max_pages", 0) or 0)
        self.max_products = int(config.scrape_settings.get("max_products", 0) or 0)
        self.expected_catalog_size = int(config.scrape_settings.get("expected_catalog_size", 4973))

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
        diagnostics: list[dict[str, str]] = []
        async with async_playwright() as playwright:
            context = await playwright.request.new_context(base_url=self.base_url, user_agent=USER_AGENT, extra_http_headers={"Accept": "text/html,application/xhtml+xml", "Accept-Language": "de-CH,de;q=0.9"})
            try:
                fetcher = Fetcher(context, raw_html_dir=self.raw_html_dir, cache_dir=self.cache_dir, min_delay_seconds=self.min_delay_seconds, max_delay_seconds=self.max_delay_seconds)
                product_urls = await self._discover_product_urls(fetcher, failures, diagnostics, logger, force_refresh=force_refresh)
                if not product_urls:
                    raise ScraperError("No Fideco products were discovered from the public product-group navigation.")
                if self.max_products:
                    product_urls = product_urls[: self.max_products]
                products, raw_count, interpreted_count = await self._fetch_products(product_urls, fetcher, failures, logger, force_refresh=force_refresh)
            finally:
                await context.dispose()
        logger.info("Fideco scrape completed. discovered=%s parsed=%s failures=%s", len(product_urls), len(products), len(failures))
        return SupplierScrapeResult(products=products, failures=failures, discovered_product_urls=set(product_urls), listing_diagnostics=diagnostics, covered_product_url_count=len(products), raw_record_count=raw_count, interpreted_record_count=interpreted_count)

    async def _discover_product_urls(self, fetcher: Fetcher, failures: list[dict[str, str]], diagnostics: list[dict[str, str]], logger: logging.Logger, *, force_refresh: bool) -> list[str]:
        frontier = [self.listing_url]
        seen_pages: set[str] = set()
        seen_categories: set[str] = set()
        product_urls: dict[str, str] = {}
        while frontier:
            batch: list[str] = []
            while frontier and len(batch) < self.category_concurrency:
                page_url = frontier.pop(0)
                if page_url in seen_pages:
                    continue
                if self.max_pages and len(seen_pages) + len(batch) >= self.max_pages:
                    break
                seen_pages.add(page_url)
                batch.append(page_url)
            if not batch:
                break

            # Listing pages are independent. Bounded parallelism keeps discovery fast
            # while preserving the site-friendly request rate in Fetcher.
            results = await asyncio.gather(
                *(fetcher.fetch_text(url, force_refresh=force_refresh) for url in batch),
                return_exceptions=True,
            )
            for page_url, result in zip(batch, results, strict=True):
                if isinstance(result, Exception):
                    failures.append({"stage": "listing", "url": page_url, "reason": str(result)})
                    continue
                html = result
                category_url = page_url.split("?", 1)[0]
                if category_url not in seen_categories:
                    seen_categories.add(category_url)
                    for child_url in extract_category_links(html, self.base_url):
                        if child_url not in seen_categories and child_url not in frontier:
                            frontier.append(child_url)
                    page_count = extract_listing_page_count(html)
                    for page in range(2, page_count + 1):
                        paged_url = listing_page_url(category_url, page)
                        if paged_url not in seen_pages and paged_url not in frontier:
                            frontier.append(paged_url)
                found_urls = extract_product_links(html, self.base_url)
                added = 0
                for url in found_urls:
                    key = product_identifier_from_url(url) or url
                    if key not in product_urls:
                        product_urls[key] = url
                        added += 1
                logger.info("Fideco listing page %s found=%s new=%s unique=%s expected=%s", len(seen_pages), len(found_urls), added, len(product_urls), self.expected_catalog_size)
                logger.info("PROGRESS phase=discovering found=%s pages=%s expected=%s", len(product_urls), len(seen_pages), self.expected_catalog_size)
                diagnostics.append({"page_url": page_url, "page_index": str(len(diagnostics) + 1), "category_root": category_url, "product_url_count": str(len(found_urls)), "cumulative_product_url_count": str(len(product_urls)), "expected_product_count": str(self.expected_catalog_size)})
        return sorted(product_urls.values())

    async def _fetch_products(self, product_urls: list[str], fetcher: Fetcher, failures: list[dict[str, str]], logger: logging.Logger, *, force_refresh: bool) -> tuple[list, int, int]:
        semaphore = asyncio.Semaphore(self.detail_concurrency)
        total = len(product_urls)
        processed = 0
        scraped = 0
        counter_lock = asyncio.Lock()
        logger.info("PROGRESS phase=processing found=%s processed=0 scraped=0 total=%s", total, total)

        async def fetch_one(url: str):
            nonlocal processed, scraped
            product = None
            try:
                async with semaphore:
                    product = parse_product_record(await fetcher.fetch_text(url, force_refresh=force_refresh), url)
                    return product
            finally:
                async with counter_lock:
                    processed += 1
                    if product is not None:
                        scraped += 1
                    logger.info("PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s", total, processed, scraped, total)

        results = await asyncio.gather(*(fetch_one(url) for url in product_urls), return_exceptions=True)
        products = []
        raw_count = 0
        interpreted_count = 0
        for url, result in zip(product_urls, results, strict=True):
            if isinstance(result, Exception):
                failures.append({"stage": "detail", "url": url, "reason": str(result)})
                continue
            raw_count += 1
            if result is None:
                failures.append({"stage": "transform", "url": url, "reason": "Unable to parse Fideco product page"})
                continue
            interpreted_count += 1
            products.append(result)
        return products, raw_count, interpreted_count
