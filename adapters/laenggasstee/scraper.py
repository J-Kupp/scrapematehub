from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from playwright.async_api import APIRequestContext, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from adapters.laenggasstee.transform import parse_product_record
from config import get_cache_root, get_log_root
from core.contracts import SupplierScrapeResult


USER_AGENT = "Mozilla/5.0 (compatible; ScrapemateHub/1.0; +https://yourbarmate.com)"
REQUEST_TIMEOUT_SECONDS = 60_000


class ScraperError(RuntimeError):
    pass


def extract_storefront_config(html: str) -> tuple[str, str, str]:
    """Read Shopware's public API details from the server-rendered storefront."""
    api_match = re.search(r'SHOP_API_URL:"([^"]+)', html)
    key_match = re.search(r'accessToken:"([^"]+)', html)
    category_match = re.search(r'product-listing-([a-f0-9]{32})', html, re.IGNORECASE)
    if not api_match or not key_match or not category_match:
        raise ScraperError("Unable to discover Shopware API configuration from the category page")
    return api_match.group(1).rstrip("/"), key_match.group(1), category_match.group(1)


class LaenggassTeeAdapter(SupplierAdapter):
    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        self.listing_url = f"{self.base_url}/shop/Camellia-sinensis/"
        self.raw_json_dir = self.output_dir / "raw_json"
        self.cache_dir = get_cache_root() / config.supplier_slug
        self.log_dir = get_log_root() / config.supplier_slug
        self.log_path = self.log_dir / "scrape.log"
        self.page_size = min(max(int(config.scrape_settings.get("page_size", 100)), 1), 100)
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
        headers = {"Accept": "application/json", "Accept-Language": "de-CH,de;q=0.9"}

        async with async_playwright() as playwright:
            context = await playwright.request.new_context(
                user_agent=USER_AGENT,
                extra_http_headers=headers,
            )
            try:
                storefront_html = await self._fetch_text(context, self.listing_url)
                api_url, access_key, category_id = extract_storefront_config(storefront_html)
                products = await self._fetch_listing(
                    context, api_url, access_key, category_id, failures, listing_diagnostics, logger,
                    force_refresh=force_refresh,
                )
            finally:
                await context.dispose()

        normalized = []
        for product in products:
            parsed = parse_product_record(product, self.base_url)
            if parsed is None:
                failures.append({"stage": "transform", "url": "", "reason": "Missing Shopware product ID or name"})
                continue
            normalized.append(parsed)
        urls = {product.canonical_url for product in normalized}
        logger.info("Laenggass-Tee scrape completed. discovered=%s parsed=%s failures=%s", len(products), len(normalized), len(failures))
        return SupplierScrapeResult(
            products=normalized,
            failures=failures,
            discovered_product_urls=urls,
            listing_diagnostics=listing_diagnostics,
            covered_product_url_count=len(normalized),
            raw_record_count=len(products),
            interpreted_record_count=len(normalized),
        )

    async def _fetch_text(self, context: APIRequestContext, url: str) -> str:
        response = await context.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        if not response.ok:
            raise ScraperError(f"HTTP {response.status} for {url}")
        return await response.text()

    async def _fetch_listing(
        self,
        context: APIRequestContext,
        api_url: str,
        access_key: str,
        category_id: str,
        failures: list[dict[str, str]],
        listing_diagnostics: list[dict[str, str]],
        logger: logging.Logger,
        *,
        force_refresh: bool,
    ) -> list[dict[str, Any]]:
        all_products: list[dict[str, Any]] = []
        page = 1
        total = 0
        while True:
            if self.max_pages and page > self.max_pages:
                break
            try:
                payload = await self._fetch_page(
                    context, api_url, access_key, category_id, page, force_refresh=force_refresh
                )
            except Exception as exc:
                failures.append({"stage": "listing", "url": self.listing_url, "reason": str(exc)})
                break
            elements = [item for item in payload.get("elements", []) if isinstance(item, dict)]
            total = int(payload.get("total", 0) or total)
            all_products.extend(elements)
            listing_diagnostics.append({
                "page_index": str(page),
                "page_url": f"{self.listing_url}?page={page}",
                "product_url_count": str(len(elements)),
                "cumulative_product_url_count": str(len(all_products)),
                "expected_product_count": str(total),
            })
            logger.info("PROGRESS phase=discovering found=%s pages=%s expected=%s", len(all_products), page, total)
            logger.info("PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s", len(all_products), len(all_products), len(all_products), total)
            if not elements or len(all_products) >= total:
                break
            page += 1

        unique_products = {str(product.get("id")): product for product in all_products if product.get("id")}
        records = list(unique_products.values())
        if self.max_products:
            records = records[: self.max_products]
        return records

    async def _fetch_page(
        self,
        context: APIRequestContext,
        api_url: str,
        access_key: str,
        category_id: str,
        page: int,
        *,
        force_refresh: bool,
    ) -> dict[str, Any]:
        snapshot = self.raw_json_dir / f"listing-page-{page}.json"
        if snapshot.exists() and not force_refresh:
            return json.loads(snapshot.read_text(encoding="utf-8"))
        body = {
            "page": page,
            "limit": self.page_size,
            "associations": {
                "cover": {"associations": {"media": {}}},
                "media": {"associations": {"media": {}}},
                "categories": {},
                "seoUrls": {},
                "manufacturer": {},
                "properties": {},
            },
        }
        endpoint = f"{api_url}/product-listing/{category_id}"
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(Exception), reraise=True,
        ):
            with attempt:
                response = await context.post(
                    endpoint,
                    data=body,
                    headers={"sw-access-key": access_key, "Accept": "application/json"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                if not response.ok:
                    raise ScraperError(f"HTTP {response.status} for Shopware listing page {page}")
                payload = await response.json()
                if not isinstance(payload, dict):
                    raise ScraperError(f"Invalid Shopware response for listing page {page}")
                snapshot.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                return payload
        raise ScraperError(f"Could not fetch Shopware listing page {page}")
