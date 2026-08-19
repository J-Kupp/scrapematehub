from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from collections import defaultdict
from pathlib import Path

from playwright.async_api import APIRequestContext, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from adapters.walker.transform import (
    extract_german_product_urls,
    extract_sitemap_urls,
    parse_product_record,
)
from config import get_log_root
from core.contracts import SupplierScrapeResult
from models import NormalizedProduct


USER_AGENT = "YourBarMateWalkerCatalog/1.0 (+https://yourbarmate.com)"
REQUEST_TIMEOUT_MS = 60_000
ROBOTS_BLOCK_REASON = (
    "Walker robots.txt disallows automated crawling. Obtain written supplier permission "
    "before setting scrape_settings.allow_robots_override=true."
)


def robots_allows_automated_access(robots_text: str) -> bool:
    active_for_all = False
    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = [part.strip() for part in line.split(":", 1)]
        field = field.lower()
        if field == "user-agent":
            active_for_all = value == "*"
            continue
        if active_for_all and field == "disallow" and value in {"/", "*", "/*"}:
            return False
    return True


class WalkerAdapter(SupplierAdapter):
    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        self.raw_html_dir = self.output_dir / "raw_html"
        self.raw_json_dir = self.output_dir / "raw_json"
        self.log_path = get_log_root() / config.supplier_slug / "scrape.log"
        self.concurrency = int(config.scrape_settings.get("concurrency", 2))
        self.min_delay_seconds = float(config.scrape_settings.get("min_delay_seconds", 0.5))
        self.max_delay_seconds = float(config.scrape_settings.get("max_delay_seconds", 1.0))
        self.allow_robots_override = bool(config.scrape_settings.get("allow_robots_override", False))
        self.max_products = int(config.scrape_settings.get("max_products", 0))

    def setup_logger(self) -> logging.Logger:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
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

    def snapshot_path(self, url: str, *, html: bool) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        directory = self.raw_html_dir if html else self.raw_json_dir
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{digest}{'.html' if html else '.xml'}"

    async def fetch_text(
        self,
        request_context: APIRequestContext,
        url: str,
        *,
        html: bool,
        force_refresh: bool,
    ) -> str:
        snapshot = self.snapshot_path(url, html=html)
        if snapshot.exists() and not force_refresh:
            return snapshot.read_text(encoding="utf-8", errors="replace")

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
                    raise RuntimeError(f"HTTP {response.status} for {url}")
                text = await response.text()
                snapshot.write_text(text, encoding="utf-8")
                return text
        raise RuntimeError(f"Failed to fetch {url}")

    @staticmethod
    def dedupe_records(records: list[NormalizedProduct], logger: logging.Logger) -> list[NormalizedProduct]:
        by_key: dict[str, NormalizedProduct] = {}
        duplicate_counts: defaultdict[str, int] = defaultdict(int)
        for record in records:
            key = record.sku or record.canonical_url
            if key in by_key:
                duplicate_counts[key] += 1
                logger.warning("Duplicate Walker product skipped for key=%s", key)
                continue
            by_key[key] = record
        return list(by_key.values())

    async def scrape(self, *, force_refresh: bool = False) -> SupplierScrapeResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        self.raw_json_dir.mkdir(parents=True, exist_ok=True)
        logger = self.setup_logger()
        logger.info("Walker scrape started.")

        async with async_playwright() as playwright:
            request_context = await playwright.request.new_context(
                base_url=self.base_url,
                user_agent=USER_AGENT,
                extra_http_headers={"Accept-Language": "de-CH,de;q=0.9"},
            )
            robots_response = await request_context.get(f"{self.base_url}/robots.txt", timeout=REQUEST_TIMEOUT_MS)
            robots_text = await robots_response.text()
            (self.raw_json_dir / "robots.txt").write_text(robots_text, encoding="utf-8")
            robots_allowed = robots_allows_automated_access(robots_text)
            diagnostics = [{
                "url": f"{self.base_url}/robots.txt",
                "robots_allowed": str(robots_allowed).lower(),
                "robots_override": str(self.allow_robots_override).lower(),
            }]

            if not robots_allowed and not self.allow_robots_override:
                logger.warning(ROBOTS_BLOCK_REASON)
                await request_context.dispose()
                raise RuntimeError(ROBOTS_BLOCK_REASON)

            sitemap_index_url = f"{self.base_url}/sitemap.xml"
            sitemap_index = await self.fetch_text(
                request_context,
                sitemap_index_url,
                html=False,
                force_refresh=force_refresh,
            )
            sitemap_urls = extract_sitemap_urls(sitemap_index)
            product_urls: set[str] = set()
            for sitemap_url in sitemap_urls:
                sitemap_text = await self.fetch_text(
                    request_context,
                    sitemap_url,
                    html=False,
                    force_refresh=force_refresh,
                )
                product_urls.update(extract_german_product_urls(sitemap_text))

            selected_urls = sorted(product_urls)
            if self.max_products > 0:
                selected_urls = selected_urls[: self.max_products]
            diagnostics.append({
                "url": sitemap_index_url,
                "sitemap_count": str(len(sitemap_urls)),
                "discovered_product_count": str(len(product_urls)),
                "selected_product_count": str(len(selected_urls)),
            })

            records: list[NormalizedProduct] = []
            failures: list[dict[str, str]] = []
            semaphore = asyncio.Semaphore(self.concurrency)

            async def worker(url: str) -> None:
                async with semaphore:
                    try:
                        html = await self.fetch_text(
                            request_context,
                            url,
                            html=True,
                            force_refresh=force_refresh,
                        )
                        record = parse_product_record(html, url)
                        if record is None:
                            failures.append({"url": url, "reason": "not-a-product-page"})
                            return
                        records.append(record)
                    except Exception as exc:
                        failures.append({"url": url, "reason": str(exc)})

            await asyncio.gather(*(worker(url) for url in selected_urls))
            await request_context.dispose()

        records = self.dedupe_records(records, logger)
        records.sort(key=lambda item: (item.category_path, item.item_name, item.sku))
        logger.info("Walker scrape finished with %s products and %s failures.", len(records), len(failures))
        return SupplierScrapeResult(
            products=records,
            failures=failures,
            discovered_product_urls=set(selected_urls),
            listing_diagnostics=diagnostics,
            covered_product_url_count=len(records) + len(failures),
            raw_record_count=len(records) + len(failures),
            interpreted_record_count=len(records),
        )


__all__ = ["ROBOTS_BLOCK_REASON", "WalkerAdapter", "robots_allows_automated_access"]
