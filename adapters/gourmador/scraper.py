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
from adapters.gourmador.transform import (
    extract_catalog_categories,
    extract_pagination_links,
    extract_product_links,
    extract_product_payload,
    parse_product_record,
)
from config import get_cache_root, get_log_root
from core.contracts import SupplierScrapeResult
from models import NormalizedProduct


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SECONDS = 60.0


class ScraperError(RuntimeError):
    pass


class Fetcher:
    def __init__(
        self,
        request_context: APIRequestContext,
        logger: logging.Logger,
        *,
        raw_html_dir: Path,
        cache_dir: Path,
        min_delay_seconds: float,
        max_delay_seconds: float,
    ) -> None:
        self.request_context = request_context
        self.logger = logger
        self.raw_html_dir = raw_html_dir
        self.cache_manifest = cache_dir / "manifest.jsonl"
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        cache_dir.mkdir(parents=True, exist_ok=True)

    async def delay(self) -> None:
        await asyncio.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))

    def snapshot_path(self, url: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", url.split("://", 1)[-1])[:120]
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        return self.raw_html_dir / f"{digest}_{slug}.html"

    async def fetch_text(self, url: str, *, force_refresh: bool = False) -> str:
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
                await self.delay()
                response = await self.request_context.get(url, timeout=int(REQUEST_TIMEOUT_SECONDS * 1000))
                if not response.ok:
                    raise ScraperError(f"HTTP {response.status} for {url}")
                text = await response.text()
                snapshot_path.write_text(text, encoding="utf-8")
                self._append_manifest(url, snapshot_path, response.status, response.url)
                return text
        raise ScraperError(f"Failed to fetch {url}")

    def _append_manifest(self, url: str, snapshot_path: Path, status_code: int, final_url: str) -> None:
        payload = {
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": "html",
            "status_code": status_code,
            "snapshot_path": str(snapshot_path),
            "url": url,
            "final_url": final_url,
        }
        with self.cache_manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


class GourmadorAdapter(SupplierAdapter):
    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        self.portal_url = f"{self.base_url}/portal?vendor=3852"
        self.raw_html_dir = self.output_dir / "raw_html"
        self.raw_json_dir = self.output_dir / "raw_json"
        self.cache_dir = get_cache_root() / config.supplier_slug
        self.log_dir = get_log_root() / config.supplier_slug
        self.log_path = self.log_dir / "scrape.log"
        self.category_concurrency = int(config.scrape_settings.get("category_concurrency", 2))
        self.detail_concurrency = int(config.scrape_settings.get("concurrency", 4))
        self.min_delay_seconds = float(config.scrape_settings.get("min_delay_seconds", 0.1))
        self.max_delay_seconds = float(config.scrape_settings.get("max_delay_seconds", 0.3))
        self.max_categories = int(config.scrape_settings.get("max_categories", 0) or 0)
        self.max_products = int(config.scrape_settings.get("max_products", 0) or 0)

    def setup_logger(self) -> logging.Logger:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"supplier_scraper.{self.config.supplier_slug}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        file_handler = logging.FileHandler(self.log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)
        return logger

    async def scrape(
        self,
        *,
        force_refresh: bool = False,
    ) -> SupplierScrapeResult:
        logger = self.setup_logger()
        self.raw_json_dir.mkdir(parents=True, exist_ok=True)
        failures: list[dict[str, str]] = []
        listing_diagnostics: list[dict[str, str]] = []

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        }
        async with async_playwright() as playwright:
            request_context = await playwright.request.new_context(
                base_url=self.base_url,
                user_agent=USER_AGENT,
                extra_http_headers=headers,
            )
            try:
                fetcher = Fetcher(
                    request_context,
                    logger,
                    raw_html_dir=self.raw_html_dir,
                    cache_dir=self.cache_dir,
                    min_delay_seconds=self.min_delay_seconds,
                    max_delay_seconds=self.max_delay_seconds,
                )
                portal_html = await fetcher.fetch_text(self.portal_url, force_refresh=force_refresh)
                categories = extract_catalog_categories(portal_html, self.base_url)
                if self.max_categories > 0:
                    categories = categories[: self.max_categories]

                discovered_urls = await self._discover_product_urls(
                    categories,
                    fetcher,
                    failures,
                    listing_diagnostics,
                    force_refresh=force_refresh,
                )
                if self.max_products > 0:
                    discovered_urls = set(sorted(discovered_urls)[: self.max_products])

                products, raw_record_count, interpreted_record_count = await self._fetch_products(
                    sorted(discovered_urls),
                    fetcher,
                    failures,
                    force_refresh=force_refresh,
                )
            finally:
                await request_context.dispose()

        logger.info(
            "Gourmador scrape completed. categories=%s discovered=%s parsed=%s failures=%s",
            len(categories),
            len(discovered_urls),
            len(products),
            len(failures),
        )
        return SupplierScrapeResult(
            products=products,
            failures=failures,
            discovered_product_urls=discovered_urls,
            listing_diagnostics=listing_diagnostics,
            covered_product_url_count=len(products),
            raw_record_count=raw_record_count,
            interpreted_record_count=interpreted_record_count,
        )

    async def _discover_product_urls(
        self,
        categories: list[dict[str, str]],
        fetcher: Fetcher,
        failures: list[dict[str, str]],
        listing_diagnostics: list[dict[str, str]],
        *,
        force_refresh: bool,
    ) -> set[str]:
        semaphore = asyncio.Semaphore(self.category_concurrency)

        async def crawl_category(category: dict[str, str]) -> tuple[set[str], dict[str, str]]:
            async with semaphore:
                to_visit = [category["category_url"]]
                seen_pages: set[str] = set()
                product_urls: set[str] = set()
                while to_visit:
                    page_url = to_visit.pop(0)
                    if page_url in seen_pages:
                        continue
                    seen_pages.add(page_url)
                    html = await fetcher.fetch_text(page_url, force_refresh=force_refresh)
                    product_urls.update(extract_product_links(html, self.base_url))
                    for next_url in extract_pagination_links(html, self.base_url):
                        if next_url not in seen_pages:
                            to_visit.append(next_url)
                diagnostic = {
                    "category_id": category["category_id"],
                    "category_name": category["category_name"],
                    "category_url": category["category_url"],
                    "parent_category_name": category["parent_category_name"],
                    "page_count": str(len(seen_pages)),
                    "product_url_count": str(len(product_urls)),
                }
                return product_urls, diagnostic

        tasks = [crawl_category(category) for category in categories]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        discovered_urls: set[str] = set()
        for category, result in zip(categories, results, strict=True):
            if isinstance(result, Exception):
                failures.append(
                    {
                        "stage": "listing",
                        "url": category["category_url"],
                        "reason": str(result),
                    }
                )
                listing_diagnostics.append(
                    {
                        "category_id": category["category_id"],
                        "category_name": category["category_name"],
                        "category_url": category["category_url"],
                        "parent_category_name": category["parent_category_name"],
                        "page_count": "0",
                        "product_url_count": "0",
                        "error": str(result),
                    }
                )
                continue
            product_urls, diagnostic = result
            discovered_urls.update(product_urls)
            listing_diagnostics.append(diagnostic)
        return discovered_urls

    async def _fetch_products(
        self,
        product_urls: list[str],
        fetcher: Fetcher,
        failures: list[dict[str, str]],
        *,
        force_refresh: bool,
    ) -> tuple[list[NormalizedProduct], int, int]:
        semaphore = asyncio.Semaphore(self.detail_concurrency)

        async def fetch_product(url: str) -> tuple[NormalizedProduct | None, dict[str, Any] | None]:
            async with semaphore:
                html = await fetcher.fetch_text(url, force_refresh=force_refresh)
                payload = extract_product_payload(html)
                if payload:
                    self._write_payload_snapshot(url, payload)
                product = parse_product_record(html, url, product_payload=payload)
                return product, payload

        tasks = [fetch_product(url) for url in product_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        products: list[NormalizedProduct] = []
        raw_record_count = 0
        interpreted_record_count = 0
        for url, result in zip(product_urls, results, strict=True):
            if isinstance(result, Exception):
                failures.append({"stage": "detail", "url": url, "reason": str(result)})
                continue
            raw_record_count += 1
            product, payload = result
            if payload is None:
                failures.append({"stage": "detail", "url": url, "reason": "Missing embedded product payload"})
                continue
            if product is None:
                failures.append({"stage": "transform", "url": url, "reason": "Unable to parse product"})
                continue
            interpreted_record_count += 1
            products.append(product)
        return products, raw_record_count, interpreted_record_count

    def _write_payload_snapshot(self, url: str, payload: dict[str, object]) -> None:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        parsed = re.sub(r"[^A-Za-z0-9._-]+", "-", url.split("://", 1)[-1])[:120]
        path = self.raw_json_dir / f"{digest}_{parsed}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
