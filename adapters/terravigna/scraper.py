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
from adapters.terravigna.transform import (
    extract_listing_product_total,
    extract_next_listing_url,
    extract_product_links,
    parse_product_record,
)
from config import get_cache_root, get_log_root
from core.contracts import SupplierScrapeResult


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_MS = 60_000


class ScraperError(RuntimeError):
    pass


class Fetcher:
    def __init__(
        self,
        context: APIRequestContext,
        *,
        raw_html_dir: Path,
        cache_dir: Path,
        min_delay_seconds: float,
        max_delay_seconds: float,
    ) -> None:
        self.context = context
        self.raw_html_dir = raw_html_dir
        self.cache_manifest = cache_dir / "manifest.jsonl"
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        cache_dir.mkdir(parents=True, exist_ok=True)

    def snapshot_path(self, url: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", url.split("://", 1)[-1])[:120]
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        return self.raw_html_dir / f"{digest}_{slug}.html"

    async def fetch_text(self, url: str, *, force_refresh: bool) -> str:
        snapshot_path = self.snapshot_path(url)
        if snapshot_path.exists() and not force_refresh:
            return snapshot_path.read_text(encoding="utf-8", errors="replace")

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                await asyncio.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))
                response = await self.context.get(url, timeout=REQUEST_TIMEOUT_MS)
                if not response.ok:
                    raise ScraperError(f"HTTP {response.status} for {url}")
                html = await response.text()
                snapshot_path.write_text(html, encoding="utf-8")
                with self.cache_manifest.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "url": url,
                                "status_code": response.status,
                                "snapshot_path": str(snapshot_path),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                return html
        raise ScraperError(f"Could not fetch {url}")


class TerraVignaAdapter(SupplierAdapter):
    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        self.listing_url = f"{self.base_url}/shop"
        self.raw_html_dir = self.output_dir / "raw_html"
        self.raw_json_dir = self.output_dir / "raw_json"
        self.cache_dir = get_cache_root() / config.supplier_slug
        self.log_dir = get_log_root() / config.supplier_slug
        self.log_path = self.log_dir / "scrape.log"
        self.detail_concurrency = max(1, int(config.scrape_settings.get("concurrency", 4)))
        self.min_delay_seconds = float(config.scrape_settings.get("min_delay_seconds", 0.1))
        self.max_delay_seconds = float(config.scrape_settings.get("max_delay_seconds", 0.3))
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
        self.raw_json_dir.mkdir(parents=True, exist_ok=True)
        failures: list[dict[str, str]] = []
        listing_diagnostics: list[dict[str, str]] = []
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        }

        async with async_playwright() as playwright:
            context = await playwright.request.new_context(
                base_url=self.base_url,
                user_agent=USER_AGENT,
                extra_http_headers=headers,
            )
            try:
                fetcher = Fetcher(
                    context,
                    raw_html_dir=self.raw_html_dir,
                    cache_dir=self.cache_dir,
                    min_delay_seconds=self.min_delay_seconds,
                    max_delay_seconds=self.max_delay_seconds,
                )
                product_urls = await self._discover_product_urls(
                    fetcher,
                    failures,
                    listing_diagnostics,
                    logger,
                    force_refresh=force_refresh,
                )
                if self.max_products:
                    product_urls = product_urls[: self.max_products]
                products, raw_count, interpreted_count = await self._fetch_products(
                    product_urls,
                    fetcher,
                    failures,
                    logger,
                    force_refresh=force_refresh,
                )
            finally:
                await context.dispose()

        logger.info(
            "TerraVigna scrape completed. discovered=%s parsed=%s failures=%s",
            len(product_urls),
            len(products),
            len(failures),
        )
        return SupplierScrapeResult(
            products=products,
            failures=failures,
            discovered_product_urls=set(product_urls),
            listing_diagnostics=listing_diagnostics,
            covered_product_url_count=len(products),
            raw_record_count=raw_count,
            interpreted_record_count=interpreted_count,
        )

    async def _discover_product_urls(
        self,
        fetcher: Fetcher,
        failures: list[dict[str, str]],
        diagnostics: list[dict[str, str]],
        logger: logging.Logger,
        *,
        force_refresh: bool,
    ) -> list[str]:
        page_url = self.listing_url
        seen_pages: set[str] = set()
        product_urls: set[str] = set()
        expected_total = 0
        while page_url and page_url not in seen_pages:
            if self.max_pages and len(seen_pages) >= self.max_pages:
                break
            seen_pages.add(page_url)
            try:
                html = await fetcher.fetch_text(page_url, force_refresh=force_refresh)
            except Exception as exc:
                failures.append({"stage": "listing", "url": page_url, "reason": str(exc)})
                break
            found_urls = extract_product_links(html, self.base_url)
            before = len(product_urls)
            product_urls.update(found_urls)
            expected_total = extract_listing_product_total(html) or expected_total
            page_index = len(seen_pages)
            logger.info(
                "TerraVigna listing page %s found=%s new=%s unique=%s expected=%s",
                page_index,
                len(found_urls),
                len(product_urls) - before,
                len(product_urls),
                expected_total or "unknown",
            )
            logger.info(
                "PROGRESS phase=discovering found=%s pages=%s expected=%s",
                len(product_urls),
                page_index,
                expected_total,
            )
            diagnostics.append(
                {
                    "page_url": page_url,
                    "page_index": str(page_index),
                    "product_url_count": str(len(found_urls)),
                    "cumulative_product_url_count": str(len(product_urls)),
                    "expected_product_count": str(expected_total),
                }
            )
            page_url = extract_next_listing_url(html, self.base_url)
        return sorted(product_urls)

    async def _fetch_products(
        self,
        product_urls: list[str],
        fetcher: Fetcher,
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

        async def fetch_product(index: int, url: str):
            nonlocal processed, scraped
            product = None
            async with semaphore:
                try:
                    html = await fetcher.fetch_text(url, force_refresh=force_refresh)
                    product = parse_product_record(html, url)
                    if product is None:
                        failures.append({"stage": "transform", "url": url, "reason": "Missing product title"})
                    return product
                finally:
                    processed += 1
                    if product is not None:
                        scraped += 1
                    logger.info(
                        "PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s",
                        total,
                        processed,
                        scraped,
                        total,
                    )

        results = await asyncio.gather(
            *(fetch_product(index, url) for index, url in enumerate(product_urls, start=1)),
            return_exceptions=True,
        )
        products = []
        raw_count = 0
        interpreted_count = 0
        for url, result in zip(product_urls, results, strict=True):
            if isinstance(result, Exception):
                failures.append({"stage": "detail", "url": url, "reason": str(result)})
                continue
            raw_count += 1
            if result is not None:
                products.append(result)
                interpreted_count += 1
        return products, raw_count, interpreted_count
