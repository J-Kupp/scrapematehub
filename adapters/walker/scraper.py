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
from adapters.walker.transform import (
    extract_category_links,
    extract_manufacturer_link,
    extract_next_listing_url,
    extract_listing_product_total,
    extract_product_links,
    parse_product_record,
    product_identifier_from_url,
)
from config import get_cache_root, get_log_root
from core.contracts import SupplierScrapeResult


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


class WalkerAdapter(SupplierAdapter):
    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        self.listing_url = f"{self.base_url}/de/alle-produkte/"
        self.raw_html_dir = self.output_dir / "raw_html"
        self.raw_json_dir = self.output_dir / "raw_json"
        self.cache_dir = get_cache_root() / config.supplier_slug
        self.log_dir = get_log_root() / config.supplier_slug
        self.log_path = self.log_dir / "scrape.log"
        self.detail_concurrency = int(config.scrape_settings.get("concurrency", 4))
        self.min_delay_seconds = float(config.scrape_settings.get("min_delay_seconds", 0.1))
        self.max_delay_seconds = float(config.scrape_settings.get("max_delay_seconds", 0.3))
        self.max_pages = int(config.scrape_settings.get("max_pages", 0) or 0)
        self.max_products = int(config.scrape_settings.get("max_products", 0) or 0)
        self.fetch_external_pages = bool(config.scrape_settings.get("fetch_external_pages", True))
        self.discover_by_categories = bool(
            config.scrape_settings.get("discover_by_categories", True)
        )

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
        enrichment_failures: list[dict[str, str]] = []
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
                discovered_urls = await self._discover_product_urls(
                    fetcher,
                    failures,
                    listing_diagnostics,
                    force_refresh=force_refresh,
                )
                if self.max_products > 0:
                    discovered_urls = set(sorted(discovered_urls)[: self.max_products])

                products, raw_record_count, interpreted_record_count, enrichment_failures = await self._fetch_products(
                    sorted(discovered_urls),
                    fetcher,
                    failures,
                    enrichment_failures,
                    force_refresh=force_refresh,
                )
            finally:
                await request_context.dispose()

        logger.info(
            "Walker scrape completed. discovered=%s parsed=%s failures=%s",
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
            enrichment_failures=enrichment_failures,
        )

    async def _discover_product_urls(
        self,
        fetcher: Fetcher,
        failures: list[dict[str, str]],
        listing_diagnostics: list[dict[str, str]],
        *,
        force_refresh: bool,
    ) -> set[str]:
        to_visit = [self.listing_url]
        seen_pages: set[str] = set()
        product_urls: dict[str, str] = {}
        expected_product_count = 0

        # Walker advertises 1'001 products, but its ungrouped listing starts
        # repeating entries after roughly half the catalogue. Its category tree
        # exposes the complete public assortment and provides stable paging.
        if self.discover_by_categories:
            try:
                initial_html = await fetcher.fetch_text(
                    self.listing_url,
                    force_refresh=force_refresh,
                )
                category_urls = [
                    url
                    for url in extract_category_links(initial_html, self.base_url)
                    if url.rstrip("/") != self.listing_url.rstrip("/")
                ]
                to_visit = category_urls or to_visit
                expected_product_count = extract_listing_product_total(initial_html)
                fetcher.logger.info(
                    "Walker category discovery found %s category listing URLs (catalogue advertises %s products)",
                    len(category_urls),
                    expected_product_count or "an unknown number of",
                )
            except Exception as exc:
                failures.append(
                    {
                        "stage": "category_discovery",
                        "url": self.listing_url,
                        "reason": str(exc),
                    }
                )

        while to_visit:
            if self.max_pages and len(seen_pages) >= self.max_pages:
                break
            page_url = to_visit.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            try:
                html = await fetcher.fetch_text(page_url, force_refresh=force_refresh)
            except Exception as exc:
                failures.append({"stage": "listing", "url": page_url, "reason": str(exc)})
                continue
            found_urls = extract_product_links(html, self.base_url)
            added_count = 0
            for product_url in found_urls:
                # Walker exposes the same article through several category paths.
                # The article number in the final URL segment is the stable identity.
                product_key = product_identifier_from_url(product_url) or product_url
                if product_key not in product_urls:
                    product_urls[product_key] = product_url
                    added_count += 1
            fetcher.logger.info(
                "Walker listing page %s found=%s new=%s unique=%s expected=%s",
                len(seen_pages),
                len(found_urls),
                added_count,
                len(product_urls),
                expected_product_count or "unknown",
            )
            fetcher.logger.info(
                "PROGRESS phase=discovering found=%s pages=%s expected=%s",
                len(product_urls),
                len(seen_pages),
                expected_product_count or 0,
            )
            listing_diagnostics.append(
                {
                    "page_url": page_url,
                    "page_index": str(len(seen_pages)),
                    "category_root": page_url.split("?", 1)[0],
                    "product_url_count": str(len(found_urls)),
                    "cumulative_product_url_count": str(len(product_urls)),
                }
            )
            if expected_product_count and len(product_urls) >= expected_product_count:
                fetcher.logger.info(
                    "Walker discovery reached the advertised catalogue size (%s products).",
                    expected_product_count,
                )
                break
            next_url = extract_next_listing_url(html, self.base_url)
            if next_url and next_url not in seen_pages:
                to_visit.append(next_url)

        return set(product_urls.values())

    async def _fetch_products(
        self,
        product_urls: list[str],
        fetcher: Fetcher,
        failures: list[dict[str, str]],
        enrichment_failures: list[dict[str, str]],
        *,
        force_refresh: bool,
    ) -> tuple[list, int, int, list[dict[str, str]]]:
        semaphore = asyncio.Semaphore(self.detail_concurrency)

        total_products = len(product_urls)
        processed_count = 0
        scraped_count = 0
        fetcher.logger.info(
            "PROGRESS phase=processing found=%s processed=0 scraped=0 total=%s",
            total_products,
            total_products,
        )

        async def fetch_product(index: int, url: str):
            async with semaphore:
                nonlocal processed_count, scraped_count
                product = None
                try:
                    fetcher.logger.info(
                        "Walker fetch start %s/%s %s",
                        index,
                        total_products,
                        url,
                    )
                    html = await fetcher.fetch_text(url, force_refresh=force_refresh)
                    external_url = extract_manufacturer_link(html, self.base_url)
                    external_html = None
                    if self.fetch_external_pages and external_url and not external_url.lower().endswith(".pdf"):
                        try:
                            external_html = await fetcher.fetch_text(external_url, force_refresh=force_refresh)
                        except Exception as exc:
                            enrichment_failures.append(
                                {"stage": "external", "url": external_url, "reason": str(exc)}
                            )
                    product = parse_product_record(
                        html,
                        url,
                        external_html=external_html,
                        external_url=external_url or None,
                    )
                    if product is None:
                        fetcher.logger.warning(
                            "Walker parse failed %s/%s %s",
                            index,
                            total_products,
                            url,
                        )
                    else:
                        sku = product.sku or "n/a"
                        fetcher.logger.info(
                            "Parsed product %s/%s %s sku=%s",
                            index,
                            total_products,
                            product.product_name,
                            sku,
                        )
                    return product
                finally:
                    processed_count += 1
                    if product is not None:
                        scraped_count += 1
                    fetcher.logger.info(
                        "PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s",
                        total_products,
                        processed_count,
                        scraped_count,
                        total_products,
                    )

        tasks = [fetch_product(index, url) for index, url in enumerate(product_urls, start=1)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        products = []
        raw_record_count = 0
        interpreted_record_count = 0
        for url, result in zip(product_urls, results, strict=True):
            if isinstance(result, Exception):
                failures.append({"stage": "detail", "url": url, "reason": str(result)})
                continue
            raw_record_count += 1
            if result is None:
                failures.append({"stage": "transform", "url": url, "reason": "Unable to parse Walker product page"})
                continue
            interpreted_record_count += 1
            products.append(result)
        if enrichment_failures:
            sample = enrichment_failures[0]
            fetcher.logger.warning(
                "Walker optional manufacturer-page enrichment unavailable for %s products; sample=%s (%s)",
                len(enrichment_failures),
                sample["url"],
                sample["reason"],
            )
        return products, raw_record_count, interpreted_record_count, enrichment_failures
