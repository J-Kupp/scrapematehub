from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path

from playwright.async_api import APIRequestContext, BrowserContext, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from adapters.terravigna.transform import (
    extract_listing_product_total,
    extract_next_listing_url,
    extract_product_links,
    extract_sitemap_product_links,
    parse_graphql_product_record,
    parse_product_record,
)
from config import get_cache_root, get_log_root
from core.contracts import SupplierScrapeResult


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_MS = 60_000
GRAPHQL_QUERY = """
{
  products(search: \"\", pageSize: 2000, currentPage: 1) {
    total_count
    items {
      name sku url_key stock_status
      description { html }
      short_description { html }
      image { url label }
      media_gallery { url label position disabled }
      categories { name url_path }
      price_range { minimum_price { final_price { value currency } } }
    }
  }
}
"""


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
                catalog_records = await self._fetch_graphql_catalog(context, playwright, headers, logger)
                if catalog_records:
                    if self.max_products:
                        catalog_records = catalog_records[: self.max_products]
                    product_urls = [
                        f"{self.base_url}/{record['url_key'].lstrip('/')}"
                        for record in catalog_records
                        if record.get("url_key")
                    ]
                    logger.info(
                        "PROGRESS phase=discovering found=%s pages=1 expected=%s",
                        len(product_urls),
                        len(product_urls),
                    )
                    products, raw_count, interpreted_count = self._transform_graphql_catalog(
                        catalog_records,
                        failures,
                        logger,
                    )
                    listing_diagnostics.append(
                        {
                            "page_url": f"{self.base_url}/graphql",
                            "page_index": "1",
                            "product_url_count": str(len(product_urls)),
                            "cumulative_product_url_count": str(len(product_urls)),
                            "expected_product_count": str(len(product_urls)),
                            "source": "magento_graphql_catalog",
                        }
                    )
                    return SupplierScrapeResult(
                        products=products,
                        failures=failures,
                        discovered_product_urls=set(product_urls),
                        listing_diagnostics=listing_diagnostics,
                        covered_product_url_count=len(products) + len(failures),
                        raw_record_count=raw_count,
                        interpreted_record_count=interpreted_count,
                    )

                # This legacy HTML route remains as a documented fallback when the
                # public Magento catalog API is unavailable.
                browser = await playwright.chromium.launch(headless=True)
                browser_context = await browser.new_context(
                    user_agent=USER_AGENT,
                    extra_http_headers=headers,
                    locale="de-CH",
                )
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
                if not product_urls:
                    sitemap_url = f"{self.base_url}/sitemap.xml"
                    sitemap_xml = await fetcher.fetch_text(sitemap_url, force_refresh=force_refresh)
                    product_urls = extract_sitemap_product_links(sitemap_xml, self.base_url)
                    logger.info(
                        "TerraVigna listing was empty; sitemap fallback found %s product URLs.",
                        len(product_urls),
                    )
                    logger.info(
                        "PROGRESS phase=discovering found=%s pages=1 expected=%s",
                        len(product_urls),
                        len(product_urls),
                    )
                    listing_diagnostics.append(
                        {
                            "page_url": sitemap_url,
                            "page_index": "fallback",
                            "product_url_count": str(len(product_urls)),
                            "cumulative_product_url_count": str(len(product_urls)),
                            "expected_product_count": str(len(product_urls)),
                            "source": "sitemap_fallback",
                        }
                    )
                if not product_urls:
                    raise ScraperError("No TerraVigna product URLs were discovered from the shop or sitemap.")
                if self.max_products:
                    product_urls = product_urls[: self.max_products]
                products, raw_count, interpreted_count = await self._fetch_products(
                    product_urls,
                    fetcher,
                    failures,
                    logger,
                    browser_context,
                    force_refresh=force_refresh,
                )
            finally:
                await context.dispose()
                if "browser_context" in locals():
                    await browser_context.close()
                    await browser.close()

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

    async def _fetch_graphql_catalog(
        self,
        context: APIRequestContext,
        playwright,
        headers: dict[str, str],
        logger: logging.Logger,
    ) -> list[dict]:
        """Read the public Magento catalog after completing the site's browser check."""
        try:
            direct_response = await context.post(
                f"{self.base_url}/graphql",
                data={"query": GRAPHQL_QUERY},
                timeout=REQUEST_TIMEOUT_MS,
            )
            direct_payload = await direct_response.json()
            direct_records = direct_payload.get("data", {}).get("products", {}).get("items", [])
            if direct_response.ok and isinstance(direct_records, list):
                logger.info("TerraVigna Magento GraphQL catalog returned %s products.", len(direct_records))
                return [record for record in direct_records if isinstance(record, dict)]
        except Exception:
            # AWS receives an Anubis HTML challenge here, so continue with a browser.
            pass

        browser = None
        browser_context = None
        page = None
        try:
            browser = await playwright.chromium.launch(headless=True)
            browser_context = await browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers=headers,
                locale="de-CH",
            )
            page = await browser_context.new_page()
            # TerraVigna protects AWS IP ranges with Anubis proof-of-work. A real
            # browser completes it once and the resulting cookie authorizes GraphQL.
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
            await page.wait_for_function(
                "document.cookie.includes('techaro.lol-anubis-auth-auth')",
                timeout=30_000,
            )
            # Anubis sets its auth cookie shortly before redirecting back to the
            # requested page. Wait for that redirect before evaluating GraphQL.
            await page.wait_for_timeout(1_000)
            response = None
            for attempt in range(3):
                try:
                    response = await page.evaluate(
                        """async (query) => {
                            const result = await fetch('/graphql', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                                body: JSON.stringify({query}),
                            });
                            return {
                                status: result.status,
                                contentType: result.headers.get('content-type') || '',
                                body: await result.text(),
                            };
                        }""",
                        GRAPHQL_QUERY,
                    )
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await page.wait_for_timeout(1_000)
            assert response is not None
            if response["status"] != 200 or "json" not in response["contentType"].lower():
                raise ScraperError(
                    f"GraphQL returned HTTP {response['status']} ({response['contentType'] or 'unknown content type'})"
                )
            payload = json.loads(response["body"])
            if payload.get("errors"):
                raise ScraperError(str(payload["errors"]))
            records = payload.get("data", {}).get("products", {}).get("items", [])
            if not isinstance(records, list):
                raise ScraperError("GraphQL products.items was not a list")
            logger.info("TerraVigna Magento GraphQL catalog returned %s products.", len(records))
            return [record for record in records if isinstance(record, dict)]
        except Exception as exc:
            logger.warning("TerraVigna Magento GraphQL catalog unavailable: %s", exc)
            return []
        finally:
            if page is not None:
                await page.close()
            if browser_context is not None:
                await browser_context.close()
            if browser is not None:
                await browser.close()

    def _transform_graphql_catalog(
        self,
        records: list[dict],
        failures: list[dict[str, str]],
        logger: logging.Logger,
    ) -> tuple[list, int, int]:
        products = []
        total = len(records)
        logger.info("PROGRESS phase=processing found=%s processed=0 scraped=0 total=%s", total, total)
        for processed, record in enumerate(records, start=1):
            product = parse_graphql_product_record(record, self.base_url)
            if product is None:
                failures.append(
                    {
                        "stage": "transform",
                        "url": f"{self.base_url}/{record.get('url_key', '')}",
                        "reason": "Magento GraphQL record missing name or URL key",
                    }
                )
            else:
                products.append(product)
            if processed % 25 == 0 or processed == total:
                logger.info(
                    "PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s",
                    total,
                    processed,
                    len(products),
                    total,
                )
        return products, total, len(products)

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
        browser_context: BrowserContext,
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
                        # Some AWS-origin responses are stripped for API clients while
                        # the same public URL is available through a normal browser.
                        page = await browser_context.new_page()
                        try:
                            response = await page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
                            await page.wait_for_selector("h1.page-title, h1 .base", timeout=30_000)
                            rendered_html = await page.content()
                            product = parse_product_record(rendered_html, url)
                            logger.info(
                                "TerraVigna browser detail fallback url=%s status=%s parsed=%s",
                                url,
                                response.status if response else "unknown",
                                bool(product),
                            )
                        finally:
                            await page.close()
                    if product is None:
                        failures.append({"stage": "transform", "url": url, "reason": "Missing product title after browser fallback"})
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
