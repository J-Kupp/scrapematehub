from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from playwright.async_api import APIRequestContext, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from adapters.casadelvino.transform import detail_url, listing_item_id, parse_product_record, parse_search_page
from config import get_log_root
from core.contracts import SupplierScrapeResult

USER_AGENT = "ScrapeMateHub/1.0 (+https://yourbarmate.com)"
CATALOG_PATH = "/de/shop/suche?suchbegriff="
SEARCH_PATH = "/api/artikelsearch/search"


class PermanentHttpError(ValueError):
    pass


class CasaDelVinoAdapter(SupplierAdapter):
    def _logger(self) -> logging.Logger:
        logger = logging.getLogger(f"supplier_scraper.{self.config.supplier_slug}")
        logger.setLevel(logging.INFO)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        log_dir = get_log_root() / self.config.supplier_slug
        log_dir.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        for handler in (logging.StreamHandler(), logging.FileHandler(log_dir / "scrape.log", encoding="utf-8")):
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    async def scrape(self, *, force_refresh: bool = False) -> SupplierScrapeResult:
        del force_refresh  # No local cache is needed in the worker.
        logger = self._logger()
        settings = self.config.scrape_settings
        max_products = max(0, int(settings.get("max_products", 0)))
        max_pages = max(0, int(settings.get("max_pages", 0)))
        concurrency = min(4, max(1, int(settings.get("concurrency", 4))))
        min_delay = max(.25, float(settings.get("min_delay_seconds", .25)))
        max_delay = max(min_delay, float(settings.get("max_delay_seconds", .55)))
        base = self.config.base_url.rstrip("/")
        search_url = base + SEARCH_PATH
        catalog_url = base + CATALOG_PATH
        diagnostics: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        products = []
        discovered: dict[str, tuple[str, dict[str, Any]]] = {}
        expected = 0

        async with async_playwright() as playwright:
            request = await playwright.request.new_context(
                user_agent=USER_AGENT, timeout=30000,
                extra_http_headers={"Accept-Language": "de-CH", "Referer": catalog_url},
            )
            robots = RobotFileParser()
            try:
                robots.parse((await self._request(request, base + "/robots.txt", min_delay, max_delay)).splitlines())

                async def fetch(url: str, data: dict[str, Any] | None = None) -> Any:
                    if urlsplit(url).netloc != urlsplit(base).netloc or not robots.can_fetch(USER_AGENT, url):
                        raise PermanentHttpError(f"URL outside supplier host or disallowed by robots.txt: {url}")
                    return await self._request(request, url, min_delay, max_delay, data)

                initial = parse_search_page(await fetch(catalog_url))
                parameters = dict(initial["searchParameters"])
                parameters.update(pageSize=100, populateSearchFields=False)
                offset = 0
                expected = int(initial["totalRecordCount"])
                while offset < expected:
                    page = await fetch(search_url, {**parameters, "start": offset})
                    items = page["items"]
                    if not items:
                        raise ValueError(f"Search returned an empty page at offset {offset} of {expected}")
                    if int(page["totalRecordCount"]) != expected:
                        raise ValueError("Search total changed during discovery")
                    added = 0
                    for item in items:
                        identifier, url = listing_item_id(item), detail_url(item, base)
                        if not identifier or not url:
                            raise ValueError(f"Invalid catalog identity or detail URL at offset {offset}")
                        if identifier not in discovered:
                            discovered[identifier] = (url, item)
                            added += 1
                    if not added:
                        raise ValueError(f"Search repeated a page at offset {offset}")
                    diagnostics.append({
                        "page_url": search_url, "page_index": str(len(diagnostics)),
                        "offset": str(offset), "product_url_count": str(added),
                        "cumulative_product_url_count": str(len(discovered)),
                        "expected_product_count": str(expected), "source": "public_catalog_search",
                    })
                    offset += len(items)
                    found = min(len(discovered), max_products) if max_products else len(discovered)
                    logger.info("PROGRESS phase=discovering found=%s pages=%s expected=%s", found, len(diagnostics), expected)
                    if (max_products and found >= max_products) or (max_pages and len(diagnostics) >= max_pages):
                        break
                if not discovered:
                    raise ValueError("Casa del Vino search returned zero catalog articles")
                if not max_pages and not max_products and len(discovered) != expected:
                    raise ValueError(f"Discovered {len(discovered)} unique articles of {expected}")
                targets = list(discovered.values())
                if max_products:
                    targets = targets[:max_products]
                if len({url for url, _ in targets}) != len(targets):
                    raise ValueError("Different catalog articles share the same detail URL")
            except Exception as exc:
                failures.append({"stage": "discovery", "url": catalog_url, "reason": str(exc)})
                targets = []
                logger.error("Casa del Vino discovery failed: %s", exc)
                if not diagnostics:
                    logger.info("PROGRESS phase=discovering found=0 pages=0 expected=%s", expected)

            try:
                semaphore = asyncio.Semaphore(concurrency)
                processed = 0
                total = len(targets)
                logger.info("PROGRESS phase=processing found=%s processed=0 scraped=0 total=%s", total, total)

                async def process(target: tuple[str, dict[str, Any]]) -> None:
                    nonlocal processed
                    url, item = target
                    try:
                        async with semaphore:
                            product = parse_product_record(await fetch(url), item, url)
                        products.append(product)
                    except Exception as exc:
                        failures.append({"stage": "detail", "url": url, "reason": str(exc)})
                    finally:
                        processed += 1
                        logger.info("PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s",
                                    total, processed, len(products), total)

                for offset in range(0, total, 80):
                    await asyncio.gather(*(process(target) for target in targets[offset:offset + 80]))
            finally:
                await request.dispose()

        urls = {url for url, _ in targets}
        logger.info("Casa del Vino completed: discovered=%s parsed=%s failures=%s", len(urls), len(products), len(failures))
        return SupplierScrapeResult(
            products=products, failures=failures, discovered_product_urls=urls,
            listing_diagnostics=diagnostics,
            covered_product_url_count=len(products) + sum(f["stage"] == "detail" for f in failures),
            raw_record_count=len(products), interpreted_record_count=len(products),
        )

    @staticmethod
    async def _request(
        request: APIRequestContext, url: str, min_delay: float, max_delay: float,
        data: dict[str, Any] | None = None,
    ) -> Any:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((RuntimeError, OSError)),
            stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True,
        ):
            with attempt:
                await asyncio.sleep(random.uniform(min_delay, max_delay))
                try:
                    response = await (
                        request.get(url, max_redirects=0) if data is None
                        else request.post(url, data=data, max_redirects=0)
                    )
                except Exception as exc:
                    raise RuntimeError(f"Request failed for {url}: {type(exc).__name__}") from exc
                try:
                    if response.status == 429 or response.status >= 500:
                        raise RuntimeError(f"HTTP {response.status} for {url}")
                    if not response.ok:
                        raise PermanentHttpError(f"HTTP {response.status} for {url}")
                    return await response.text() if data is None else await response.json()
                finally:
                    await response.dispose()
