from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Any

from playwright.async_api import APIRequestContext, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from adapters.merat.transform import discover_product_urls, parse_product_record, product_identifier
from config import get_log_root
from core.contracts import SupplierScrapeResult


USER_AGENT = "Mozilla/5.0 (compatible; ScrapeMateHub/1.0; +https://yourbarmate.com)"
REQUEST_TIMEOUT_MS = 60_000
DEFAULT_PAGE_SIZE = 100


class ScraperError(RuntimeError):
    pass


class MeratAdapter(SupplierAdapter):
    """Fetch the public Merat catalog API and transform its public product records."""

    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        self.api_base = f"{self.base_url}/portal/api"
        self.log_dir = get_log_root() / config.supplier_slug
        self.log_path = self.log_dir / "scrape.log"
        self.concurrency = max(1, int(config.scrape_settings.get("concurrency", 3)))
        self.min_delay_seconds = float(config.scrape_settings.get("min_delay_seconds", 0.2))
        self.max_delay_seconds = float(config.scrape_settings.get("max_delay_seconds", 0.45))
        self.page_size = min(100, max(1, int(config.scrape_settings.get("page_size", DEFAULT_PAGE_SIZE))))
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
        del force_refresh  # The Merat catalog API is live data and is never served from a local cache.
        logger = self.setup_logger()
        failures: list[dict[str, str]] = []
        diagnostics: list[dict[str, str]] = []
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.7",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/portal/app/catalog/category",
        }

        async with async_playwright() as playwright:
            context = await playwright.request.new_context(
                user_agent=USER_AGENT,
                extra_http_headers=headers,
                timeout=REQUEST_TIMEOUT_MS,
            )
            try:
                await self._check_robots(context)
                listing_records = await self._discover(context, failures, diagnostics, logger)
                if self.max_products:
                    listing_records = listing_records[: self.max_products]
                urls = {
                    f"{self.base_url}/portal/app/catalog/category/product/{sku}"
                    for record in listing_records
                    if (sku := product_identifier(record))
                }
                products = await self._fetch_products(context, listing_records, failures, logger)
            except Exception as exc:
                logger.exception("Merat scrape failed before completion")
                failures.append({"stage": "scrape", "url": self.base_url, "reason": str(exc)})
                listing_records = []
                urls = set()
                products = []
            finally:
                await context.dispose()

        if not urls and not any(failure.get("stage") == "discovery" for failure in failures):
            failures.append({
                "stage": "discovery",
                "url": f"{self.api_base}/catalog/search_products?locale=de",
                "reason": "Merat returned no products from its anonymous catalog search API",
            })
        logger.info("Merat scrape completed. discovered=%s parsed=%s failures=%s", len(urls), len(products), len(failures))
        return SupplierScrapeResult(
            products=products,
            failures=failures,
            discovered_product_urls=urls,
            listing_diagnostics=diagnostics,
            covered_product_url_count=len(products) + sum(failure.get("stage") == "detail" for failure in failures),
            raw_record_count=len(listing_records),
            interpreted_record_count=len(products),
        )

    async def _check_robots(self, context: APIRequestContext) -> None:
        response = await context.get(f"{self.base_url}/robots.txt")
        if not response.ok:
            raise ScraperError(f"Could not verify robots.txt: HTTP {response.status}")
        lines = (await response.text()).splitlines()
        active_for_all = False
        disallowed: list[str] = []
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            if key.lower() == "user-agent":
                active_for_all = value == "*"
            elif active_for_all and key.lower() == "disallow" and value:
                disallowed.append(value)
        for path in ("/portal/api/catalog/list_categories", "/portal/api/catalog/search_products", "/portal/api/catalog/get_product"):
            if any(path.startswith(rule) for rule in disallowed):
                raise ScraperError(f"robots.txt disallows the required public catalog endpoint: {path}")

    async def _request_json(self, context: APIRequestContext, url: str, *, data: dict[str, Any] | None = None) -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                await asyncio.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))
                if data is None:
                    response = await context.get(url, timeout=REQUEST_TIMEOUT_MS)
                else:
                    response = await context.post(url, data=data, timeout=REQUEST_TIMEOUT_MS)
                if not response.ok:
                    raise ScraperError(f"HTTP {response.status} for {url}")
                try:
                    return await response.json()
                except Exception as exc:
                    raise ScraperError(f"Invalid JSON response from {url}: {exc}") from exc
        raise ScraperError(f"Could not fetch {url}")

    async def _discover(
        self,
        context: APIRequestContext,
        failures: list[dict[str, str]],
        diagnostics: list[dict[str, str]],
        logger: logging.Logger,
    ) -> list[dict[str, Any]]:
        categories_url = f"{self.api_base}/catalog/list_categories?locale=de&depth=3&includeEmpty=true"
        search_url = f"{self.api_base}/catalog/search_products?locale=de"
        try:
            categories = await self._request_json(context, categories_url)
            if not isinstance(categories, list) or not categories:
                raise ScraperError("Merat category API returned no categories")
            first = await self._request_json(
                context,
                search_url,
                data={"page": 0, "size": self.page_size, "filterAggregates": False, "totalAggregates": True},
            )
            if not isinstance(first, dict) or not isinstance(first.get("content"), list):
                raise ScraperError("Merat search API returned an unexpected page shape")
            total = int(first.get("totalElements", 0) or 0)
            total_pages = int(first.get("totalPages", 0) or 0)
            pages_to_fetch = min(total_pages, self.max_pages) if self.max_pages else total_pages
            if self.max_products:
                pages_to_fetch = min(pages_to_fetch, max(1, (self.max_products + self.page_size - 1) // self.page_size))

            records = list(first.get("content") or [])
            diagnostics.append({
                "page_url": search_url,
                "page_index": "0",
                "product_url_count": str(len(first.get("content") or [])),
                "cumulative_product_url_count": str(len(records)),
                "expected_product_count": str(total),
                "source": "merat_public_catalog_api",
            })
            found_so_far = min(len({product_identifier(record) for record in records if isinstance(record, dict)}), self.max_products or total)
            logger.info("PROGRESS phase=discovering found=%s pages=1 expected=%s", found_so_far, total)

            for page_number in range(1, pages_to_fetch):
                page = await self._request_json(
                    context,
                    search_url,
                    data={"page": page_number, "size": self.page_size, "filterAggregates": False, "totalAggregates": False},
                )
                page_records = page.get("content") or []
                records.extend(page_records)
                diagnostics.append({
                    "page_url": search_url,
                    "page_index": str(page_number),
                    "product_url_count": str(len(page_records)),
                    "cumulative_product_url_count": str(len(records)),
                    "expected_product_count": str(total),
                    "source": "merat_public_catalog_api",
                })
                found_so_far = min(
                    len({product_identifier(record) for record in records if isinstance(record, dict)}),
                    self.max_products or total,
                )
                logger.info(
                    "PROGRESS phase=discovering found=%s pages=%s expected=%s",
                    found_so_far, page_number + 1, total,
                )
            records = [record for record in records if isinstance(record, dict) and product_identifier(record)]
            deduplicated = {product_identifier(record): record for record in records}
            records = [deduplicated[key] for key in sorted(deduplicated)]
            if self.max_products:
                records = records[: self.max_products]
            if not records:
                failures.append({"stage": "discovery", "url": search_url, "reason": "Merat search returned no product records"})
            elif len(records) < total and not self.max_products:
                failures.append({
                    "stage": "discovery",
                    "url": search_url,
                    "reason": f"Merat search returned {len(records)} unique products while reporting {total}",
                })
            return records
        except Exception as exc:
            failures.append({"stage": "discovery", "url": search_url, "reason": str(exc)})
            logger.exception("Merat catalog discovery failed")
            return []

    async def _fetch_products(
        self,
        context: APIRequestContext,
        listing_records: list[dict[str, Any]],
        failures: list[dict[str, str]],
        logger: logging.Logger,
    ) -> list:
        if not listing_records:
            logger.info("PROGRESS phase=processing found=0 processed=0 scraped=0 total=0")
            return []

        semaphore = asyncio.Semaphore(self.concurrency)
        progress_lock = asyncio.Lock()
        processed = 0
        products = []

        async def fetch_one(listing_record: dict[str, Any]) -> None:
            nonlocal processed
            sku = product_identifier(listing_record)
            url = f"{self.api_base}/catalog/get_product?locale=de&itemNumber={sku}"
            try:
                async with semaphore:
                    detail = await self._request_json(context, url)
                product = parse_product_record(detail, self.base_url, listing_record)
                if product is None:
                    raise ScraperError("Merat detail record did not contain an item number and name")
                products.append(product)
            except Exception as exc:
                failures.append({"stage": "detail", "url": url, "reason": str(exc)})
            finally:
                async with progress_lock:
                    processed += 1
                    logger.info(
                        "PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s",
                        len(listing_records), processed, len(products), len(listing_records),
                    )

        await asyncio.gather(*(fetch_one(record) for record in listing_records))
        return sorted(products, key=lambda product: product.sku)


__all__ = ["MeratAdapter", "ScraperError"]
