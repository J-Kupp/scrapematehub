from __future__ import annotations

import asyncio
import logging
import random
import re
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from adapters.base import SupplierAdapter
from adapters.divina.transform import (
    extract_listing_total, extract_next_listing_url, extract_product_links, parse_product_record,
)
from config import get_log_root
from core.contracts import SupplierScrapeResult

USER_AGENT = 'ScrapeMateHub/1.0 (+https://yourbarmate.com)'


def robots_allows(robots: str, url: str) -> bool:
    """Apply wildcard rules in the site's generic crawler group, longest match first."""
    active = False
    matches = []
    parts = urlsplit(url)
    path = parts.path + ('?' + parts.query if parts.query else '')
    for line in robots.splitlines():
        line = line.split('#', 1)[0].strip()
        if ':' not in line:
            continue
        key, value = (part.strip() for part in line.split(':', 1))
        key = key.lower()
        if key == 'user-agent':
            active = value == '*'
        elif active and key in {'allow', 'disallow'} and value:
            pattern = '^' + re.escape(value).replace(r'\*', '.*').replace(r'\$', '$')
            if re.search(pattern, path):
                matches.append((len(value.replace('*', '')), key == 'allow'))
    return max(matches, default=(0, True))[1]


class DivinaAdapter(SupplierAdapter):
    async def scrape(self, *, force_refresh: bool = False) -> SupplierScrapeResult:
        del force_refresh  # Fetch current public HTML on every run, no local prerequisites.
        settings = self.config.scrape_settings
        concurrency = min(6, max(1, int(settings.get('concurrency', 3))))
        max_products = int(settings.get('max_products', 0) or 0)
        max_pages = int(settings.get('max_pages', 0) or 0)
        min_delay = max(0.1, float(settings.get('min_delay_seconds', .2)))
        max_delay = max(min_delay, float(settings.get('max_delay_seconds', .5)))
        base = self.config.base_url.rstrip('/')
        logger = logging.getLogger('supplier_scraper.' + self.config.supplier_slug)
        logger.setLevel(logging.INFO)
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
        log_dir = get_log_root() / self.config.supplier_slug
        log_dir.mkdir(parents=True, exist_ok=True)
        for handler in (logging.StreamHandler(), logging.FileHandler(log_dir / 'scrape.log', encoding='utf-8')):
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            logger.addHandler(handler)
        failures, diagnostics, products = [], [], []
        urls: set[str] = set()
        expected = 0
        async with async_playwright() as playwright:
            context = await playwright.request.new_context(user_agent=USER_AGENT, extra_http_headers={'Accept-Language': 'de-CH,de;q=0.9'})
            try:
                response = await context.get(base + '/robots.txt', timeout=60000)
                if not response.ok:
                    raise RuntimeError(f'Could not verify robots.txt: HTTP {response.status}')
                robots = await response.text()

                async def fetch(url):
                    if urlsplit(url).netloc != urlsplit(base).netloc or not robots_allows(robots, url):
                        raise RuntimeError(f'URL is outside supplier host or blocked by robots.txt: {url}')
                    async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True):
                        with attempt:
                            await asyncio.sleep(random.uniform(min_delay, max_delay))
                            result = await context.get(url, timeout=60000)
                            if not result.ok:
                                raise RuntimeError(f'HTTP {result.status} for {url}')
                            return await result.text()

                listing_url = base + '/2-alle-produkte'
                seen_pages = set()
                while listing_url and listing_url not in seen_pages:
                    seen_pages.add(listing_url)
                    html = await fetch(listing_url)
                    expected = max(expected, extract_listing_total(html))
                    page_links = extract_product_links(html, base)
                    if not page_links:
                        raise RuntimeError(f'Listing page contains no product links: {listing_url}')
                    urls.update(page_links)
                    found = min(len(urls), max_products) if max_products else len(urls)
                    diagnostics.append({'page_url': listing_url, 'product_url_count': str(len(page_links)), 'cumulative_product_url_count': str(len(urls)), 'expected_product_count': str(expected)})
                    logger.info('PROGRESS phase=discovering found=%s pages=%s expected=%s', found, len(seen_pages), expected)
                    if (max_products and len(urls) >= max_products) or (max_pages and len(seen_pages) >= max_pages):
                        break
                    listing_url = extract_next_listing_url(html, base)
                if not urls:
                    raise RuntimeError('Divina catalog discovery returned no products')
                if expected and len(urls) != expected and not max_products and not max_pages:
                    failures.append({'stage': 'discovery', 'url': base, 'reason': f'Expected {expected} products, discovered {len(urls)}'})
                if max_products:
                    urls = set(sorted(urls)[:max_products])
                semaphore = asyncio.Semaphore(concurrency)
                processed = 0

                async def detail(url):
                    nonlocal processed
                    try:
                        async with semaphore:
                            html = await fetch(url)
                            product = parse_product_record(html, url)
                            if not product:
                                raise RuntimeError('Detail page is missing the supplier SKU or name')
                            products.append(product)
                    except Exception as exc:
                        failures.append({'stage': 'detail', 'url': url, 'reason': str(exc)})
                    finally:
                        processed += 1
                        logger.info('PROGRESS phase=processing found=%s processed=%s scraped=%s total=%s', len(urls), processed, len(products), len(urls))
                await asyncio.gather(*(detail(url) for url in sorted(urls)))
            except Exception as exc:
                failures.append({'stage': 'discovery', 'url': base, 'reason': str(exc)})
                logger.error('Divina discovery failed: %s', exc)
                logger.info('PROGRESS phase=discovering found=%s pages=%s expected=%s', len(urls), len(diagnostics), expected)
                logger.info('PROGRESS phase=processing found=%s processed=0 scraped=0 total=%s', len(urls), len(urls))
            finally:
                await context.dispose()
        logger.info('Divina completed: discovered=%s parsed=%s failures=%s', len(urls), len(products), len(failures))
        return SupplierScrapeResult(
            products=products, failures=failures, discovered_product_urls=urls,
            listing_diagnostics=diagnostics,
            covered_product_url_count=len(products) + sum(f['stage'] == 'detail' for f in failures),
            raw_record_count=len(products), interpreted_record_count=len(products),
        )
