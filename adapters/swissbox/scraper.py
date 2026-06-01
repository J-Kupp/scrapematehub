from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import random
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import APIRequestContext, async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from config import get_cache_root, get_log_root
from core.contracts import SupplierScrapeResult
from models import NormalizedProduct
from adapters.swissbox.transform import parse_product_record, product_candidate_from_url


USER_AGENT = (
    "Mozilla/5.0 (compatible; SwissboxCatalogAudit/2.0; "
    "+https://www.swissbox-ag.ch)"
)
REQUEST_TIMEOUT_MS = 60_000

ROOT_CATEGORY_SLUGS = {
    "buero",
    "deko-und-ladenzubehoer",
    "essen",
    "festivals",
    "gastrobedarf",
    "hygiene-und-bedarfsartikel",
    "mehrweggeschirr-mieten",
    "oeko-line-produkte",
    "reinigung",
    "sale",
    "trinken",
    "displaymaterial",
}


class ScraperError(RuntimeError):
    pass


class RobotsRules:
    def __init__(self, text: str) -> None:
        self.raw = text
        self.disallow_patterns: list[re.Pattern[str]] = []
        self.allow_patterns: list[re.Pattern[str]] = []
        self.sitemaps: list[str] = []
        self._parse(text)

    def _parse(self, text: str) -> None:
        active_for_all = False
        active_for_other = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            field, value = [part.strip() for part in line.split(":", 1)]
            field_lower = field.lower()
            if field_lower == "user-agent":
                active_for_all = value == "*"
                active_for_other = not active_for_all
                continue
            if field_lower == "sitemap":
                self.sitemaps.append(value)
                continue
            if active_for_other or field_lower not in {"allow", "disallow"}:
                continue
            regex = self._pattern_to_regex(value)
            if field_lower == "allow":
                self.allow_patterns.append(regex)
            else:
                self.disallow_patterns.append(regex)

    @staticmethod
    def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
        if not pattern:
            return re.compile(r"$^")
        escaped = re.escape(pattern).replace(r"\*", ".*")
        if escaped.endswith(r"\$"):
            escaped = escaped[:-2] + "$"
        else:
            escaped += ".*"
        return re.compile("^" + escaped)

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        candidate = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        allow_match = max((len(match.pattern) for match in self.allow_patterns if match.search(candidate)), default=-1)
        disallow_match = max(
            (len(match.pattern) for match in self.disallow_patterns if match.search(candidate)),
            default=-1,
        )
        return allow_match >= disallow_match


class Fetcher:
    def __init__(
        self,
        request_context: APIRequestContext,
        robots: RobotsRules,
        logger: logging.Logger,
        *,
        raw_html_dir: Path,
        raw_json_dir: Path,
        cache_dir: Path,
        min_delay_seconds: float,
        max_delay_seconds: float,
    ) -> None:
        self.request_context = request_context
        self.robots = robots
        self.logger = logger
        self.raw_html_dir = raw_html_dir
        self.raw_json_dir = raw_json_dir
        self.cache_manifest = cache_dir / "manifest.jsonl"
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        cache_dir.mkdir(parents=True, exist_ok=True)

    async def delay(self) -> None:
        await asyncio.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))

    def snapshot_path(self, url: str, kind: str) -> Path:
        parsed = urlparse(url)
        slug = parsed.path.strip("/").replace("/", "__") or "homepage"
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug)[:120]
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        suffix = ".html" if kind == "html" else ".bin"
        directory = self.raw_html_dir if kind == "html" else self.raw_json_dir
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{digest}_{slug}{suffix}"

    async def fetch_text(self, url: str, *, kind: str = "html", force_refresh: bool = False) -> str:
        if not self.robots.allows(url):
            raise ScraperError(f"Blocked by robots.txt: {url}")
        snapshot_path = self.snapshot_path(url, kind)
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
                response = await self.request_context.get(url, timeout=REQUEST_TIMEOUT_MS)
                if not response.ok:
                    raise ScraperError(f"HTTP {response.status} for {url}")
                text = await response.text()
                snapshot_path.write_text(text, encoding="utf-8")
                self._append_manifest(url, snapshot_path, response.status, kind)
                return text
        raise ScraperError(f"Failed to fetch {url}")

    async def fetch_bytes(self, url: str, filename: str, *, force_refresh: bool = False) -> bytes:
        if not self.robots.allows(url):
            raise ScraperError(f"Blocked by robots.txt: {url}")
        output_path = self.raw_json_dir / filename
        if output_path.exists() and not force_refresh:
            return output_path.read_bytes()

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                await self.delay()
                response = await self.request_context.get(url, timeout=REQUEST_TIMEOUT_MS)
                if not response.ok:
                    raise ScraperError(f"HTTP {response.status} for {url}")
                body = await response.body()
                output_path.write_bytes(body)
                self._append_manifest(url, output_path, response.status, "bytes")
                return body
        raise ScraperError(f"Failed to fetch {url}")

    def _append_manifest(self, url: str, snapshot_path: Path, status_code: int, kind: str) -> None:
        payload = {
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": kind,
            "status_code": status_code,
            "snapshot_path": str(snapshot_path),
            "url": url,
        }
        with self.cache_manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


class SwissboxAdapter(SupplierAdapter):
    def __init__(self, config, project_root: Path) -> None:
        super().__init__(config, project_root)
        self.base_url = config.base_url.rstrip("/")
        self.raw_html_dir = self.output_dir / "raw_html"
        self.raw_json_dir = self.output_dir / "raw_json"
        self.cache_dir = get_cache_root() / config.supplier_slug
        self.log_dir = get_log_root() / config.supplier_slug
        self.log_path = self.log_dir / "scrape.log"
        self.concurrency = int(config.scrape_settings.get("concurrency", 2))
        self.min_delay_seconds = float(config.scrape_settings.get("min_delay_seconds", 0.15))
        self.max_delay_seconds = float(config.scrape_settings.get("max_delay_seconds", 0.45))

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

    def normalize_url(self, url: str) -> str:
        stripped = url.strip()
        if stripped.startswith("https://swissbox-ag.ch/"):
            stripped = stripped.replace("https://swissbox-ag.ch/", "https://www.swissbox-ag.ch/", 1)
        return stripped.rstrip("/") + "/"

    def is_excluded_url(self, url: str) -> bool:
        excluded_prefixes = (
            f"{self.base_url}/agb",
            f"{self.base_url}/impressum",
            f"{self.base_url}/datenschutz",
            f"{self.base_url}/checkout",
            f"{self.base_url}/account",
            f"{self.base_url}/widgets",
            f"{self.base_url}/navigation",
            f"{self.base_url}/bundles",
        )
        return any(url.startswith(prefix) for prefix in excluded_prefixes)

    def iter_navigation_links(self, homepage_html: str) -> tuple[set[str], set[str]]:
        category_urls: set[str] = set()
        footer_urls: set[str] = set()
        soup = BeautifulSoup(homepage_html, "lxml")
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            if not href.startswith("https://www.swissbox-ag.ch/"):
                continue
            href = self.normalize_url(href)
            if self.is_excluded_url(href):
                continue
            path = urlparse(href).path.strip("/")
            if not path:
                continue
            if path.split("/")[0] in ROOT_CATEGORY_SLUGS:
                category_urls.add(href)
            else:
                footer_urls.add(href)
        return category_urls, footer_urls

    def parse_listing_product_urls(self, category_html: str) -> set[str]:
        soup = BeautifulSoup(category_html, "lxml")
        return {
            self.normalize_url(anchor.get("href", ""))
            for anchor in soup.select(".product-box a.product-name[href]")
            if anchor.get("href", "").startswith("https://www.swissbox-ag.ch/")
        }

    def parse_listing_diagnostics(self, category_html: str) -> dict[str, str]:
        diagnostics: dict[str, str] = {}
        widget_match = re.search(r'"dataUrl":"([^"]+)"', category_html.replace("&quot;", '"'))
        diagnostics["has_listing_widget"] = "true" if 'data-listing="true"' in category_html else "false"
        diagnostics["uses_widget_data_url"] = widget_match.group(1) if widget_match else ""
        diagnostics["has_pagination"] = "true" if "pagination" in category_html.lower() else "false"
        diagnostics["product_card_count"] = str(category_html.count('class="card product-box'))
        return diagnostics

    @staticmethod
    def extract_sitemap_urls(xml_text: str) -> list[str]:
        return re.findall(r"<loc>(.*?)</loc>", xml_text)

    def categorize_sitemap_urls(self, urls: Iterable[str]) -> tuple[set[str], set[str]]:
        category_urls: set[str] = set()
        product_candidates: set[str] = set()
        for raw_url in urls:
            url = self.normalize_url(raw_url)
            if self.is_excluded_url(url):
                continue
            if product_candidate_from_url(url):
                product_candidates.add(url)
                continue
            path = urlparse(url).path.strip("/")
            first_segment = path.split("/", 1)[0] if path else ""
            if first_segment in ROOT_CATEGORY_SLUGS:
                category_urls.add(url)
        return category_urls, product_candidates

    def dedupe_records(self, records: list[NormalizedProduct], logger: logging.Logger) -> list[NormalizedProduct]:
        by_key: dict[str, NormalizedProduct] = {}
        duplicate_counts: defaultdict[str, int] = defaultdict(int)
        for record in records:
            key = record.sku or f"{record.canonical_url}|{json.dumps(record.variant_options, sort_keys=True, ensure_ascii=False)}"
            if key in by_key:
                duplicate_counts[key] += 1
                logger.warning("Duplicate record skipped for key=%s url=%s", key, record.product_url)
                continue
            by_key[key] = record
        return list(by_key.values())

    async def fetch_category_pages(
        self,
        fetcher: Fetcher,
        category_urls: list[str],
        logger: logging.Logger,
        *,
        force_refresh: bool,
    ) -> tuple[set[str], list[dict[str, str]]]:
        logger.info("Inspecting %s category/listing pages (first page only).", len(category_urls))
        discovered_product_urls: set[str] = set()
        diagnostics_rows: list[dict[str, str]] = []
        semaphore = asyncio.Semaphore(self.concurrency)

        async def worker(url: str) -> None:
            async with semaphore:
                try:
                    html = await fetcher.fetch_text(url, kind="html", force_refresh=force_refresh)
                except Exception as exc:
                    logger.warning("Category fetch failed for %s: %s", url, exc)
                    diagnostics_rows.append({"url": url, "error": str(exc)})
                    return
                discovered_product_urls.update(self.parse_listing_product_urls(html))
                diagnostics = self.parse_listing_diagnostics(html)
                diagnostics["url"] = url
                diagnostics_rows.append(diagnostics)
                logger.info(
                    "Category %s -> %s product cards, pagination=%s widget=%s",
                    url,
                    diagnostics.get("product_card_count", "0"),
                    diagnostics.get("has_pagination", "false"),
                    diagnostics.get("uses_widget_data_url", ""),
                )

        await asyncio.gather(*(worker(url) for url in category_urls))
        return discovered_product_urls, diagnostics_rows

    async def fetch_product_records(
        self,
        fetcher: Fetcher,
        product_urls: list[str],
        logger: logging.Logger,
        *,
        force_refresh: bool,
    ) -> tuple[list[NormalizedProduct], list[dict[str, str]]]:
        logger.info("Fetching %s candidate product URLs.", len(product_urls))
        records: list[NormalizedProduct] = []
        failures: list[dict[str, str]] = []
        semaphore = asyncio.Semaphore(self.concurrency)

        async def worker(url: str) -> None:
            async with semaphore:
                try:
                    html = await fetcher.fetch_text(url, kind="html", force_refresh=force_refresh)
                    record = parse_product_record(html, url)
                    if record is None:
                        failures.append({"url": url, "reason": "not-a-product-page"})
                        logger.info("Skipped non-product URL %s", url)
                        return
                    records.append(record)
                    logger.info("Parsed product %s sku=%s", record.item_name, record.sku or "NO_SKU")
                except Exception as exc:
                    failures.append({"url": url, "reason": str(exc)})
                    logger.warning("Product fetch/parse failed for %s: %s", url, exc)

        await asyncio.gather(*(worker(url) for url in product_urls))
        return records, failures

    async def scrape(
        self,
        *,
        force_refresh: bool = False,
    ) -> SupplierScrapeResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        self.raw_json_dir.mkdir(parents=True, exist_ok=True)
        logger = self.setup_logger()
        logger.info("Swissbox scrape started.")

        async with async_playwright() as playwright:
            request_context = await playwright.request.new_context(
                base_url=self.base_url,
                user_agent=USER_AGENT,
                extra_http_headers={"Accept-Language": "de-CH,de;q=0.9,en;q=0.8"},
            )
            robots_response = await request_context.get(f"{self.base_url}/robots.txt", timeout=REQUEST_TIMEOUT_MS)
            robots_text = await robots_response.text()
            (self.raw_json_dir / "robots.txt").write_text(robots_text, encoding="utf-8")
            robots = RobotsRules(robots_text)
            logger.info("Loaded robots.txt with %s sitemap(s).", len(robots.sitemaps))

            fetcher = Fetcher(
                request_context,
                robots,
                logger,
                raw_html_dir=self.raw_html_dir,
                raw_json_dir=self.raw_json_dir,
                cache_dir=self.cache_dir,
                min_delay_seconds=self.min_delay_seconds,
                max_delay_seconds=self.max_delay_seconds,
            )

            homepage_html = await fetcher.fetch_text(self.base_url, kind="html", force_refresh=force_refresh)
            (self.raw_html_dir / "homepage.html").write_text(homepage_html, encoding="utf-8")
            nav_category_urls, footer_links = self.iter_navigation_links(homepage_html)
            logger.info(
                "Discovered %s nav/footer category candidates and %s other footer links.",
                len(nav_category_urls),
                len(footer_links),
            )

            sitemap_urls: list[str] = []
            sitemap_index_text = await fetcher.fetch_text(robots.sitemaps[0], kind="html", force_refresh=force_refresh)
            (self.raw_json_dir / "sitemap.xml").write_text(sitemap_index_text, encoding="utf-8")
            sitemap_file_urls = self.extract_sitemap_urls(sitemap_index_text)
            logger.info("Sitemap index contains %s nested sitemap files.", len(sitemap_file_urls))

            for index, sitemap_url in enumerate(sitemap_file_urls, start=1):
                filename = f"sitemap_{index}.xml.gz"
                payload = await fetcher.fetch_bytes(sitemap_url, filename, force_refresh=force_refresh)
                xml_text = gzip.decompress(payload).decode("utf-8", errors="replace")
                (self.raw_json_dir / f"sitemap_{index}.xml").write_text(xml_text, encoding="utf-8")
                urls = self.extract_sitemap_urls(xml_text)
                sitemap_urls.extend(urls)
                logger.info("Nested sitemap %s yielded %s URLs.", sitemap_url, len(urls))

            sitemap_category_urls, sitemap_product_candidates = self.categorize_sitemap_urls(sitemap_urls)
            category_urls = sorted({*nav_category_urls, *sitemap_category_urls})
            discovered_from_categories, diagnostics_rows = await self.fetch_category_pages(
                fetcher,
                category_urls,
                logger,
                force_refresh=force_refresh,
            )

            product_url_candidates = sorted(
                {
                    *[self.normalize_url(url) for url in sitemap_product_candidates],
                    *[self.normalize_url(url) for url in discovered_from_categories if product_candidate_from_url(url)],
                }
            )
            product_url_candidates = [url for url in product_url_candidates if product_candidate_from_url(url)]
            logger.info(
                "Prepared %s candidate product URLs from sitemap/root slugs and category cards.",
                len(product_url_candidates),
            )

            records, failures = await self.fetch_product_records(
                fetcher,
                product_url_candidates,
                logger,
                force_refresh=force_refresh,
            )
            covered_product_url_count = len(records) + len(failures)
            records = self.dedupe_records(records, logger)
            records.sort(key=lambda item: (item.category_path, item.item_name, item.sku or item.canonical_url))
            await request_context.dispose()

        logger.info("Swissbox scrape finished.")
        return SupplierScrapeResult(
            products=records,
            failures=failures,
            discovered_product_urls=set(product_url_candidates),
            listing_diagnostics=diagnostics_rows,
            covered_product_url_count=covered_product_url_count,
            raw_record_count=covered_product_url_count,
            interpreted_record_count=len(records),
        )
