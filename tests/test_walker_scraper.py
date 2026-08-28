from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

from adapters.walker.scraper import WalkerAdapter
from models import SupplierConfig


PRODUCT_URL = "https://shop.walker.swiss/de/alle-produkte/obst/feigen-999.html"
EXTERNAL_URL = "https://manufacturer.example/feigen"


class FailingExternalFetcher:
    def __init__(self) -> None:
        self.logger = logging.getLogger("walker-scraper-test")

    async def fetch_text(self, url: str, *, force_refresh: bool = False) -> str:
        if url == EXTERNAL_URL:
            raise RuntimeError("HTTP 403 for optional manufacturer page")
        return """
        <div class="article-infos">
          <h1>Feigen 3 kg</h1>
          <dl class="article-spec-infos">
            <dt>Artikelnummer</dt><dd>140165</dd>
            <dt>Link</dt><dd><a href="https://manufacturer.example/feigen">Herstellerlink</a></dd>
          </dl>
        </div>
        """


class WalkerScraperTests(unittest.TestCase):
    def test_optional_external_failure_does_not_mark_product_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = SupplierConfig(
                supplier_slug="walker",
                enabled=True,
                scraper_adapter="walker",
                base_url="https://shop.walker.swiss",
                ybm_token_env_var="WALKER_TOKEN",
                output_dir="output/walker",
                scrape_settings={"fetch_external_pages": True},
            )
            adapter = WalkerAdapter(config, Path(tmpdir))
            product_failures: list[dict[str, str]] = []
            enrichment_failures: list[dict[str, str]] = []

            products, raw_count, interpreted_count, returned_enrichment_failures = asyncio.run(
                adapter._fetch_products(
                    [PRODUCT_URL],
                    FailingExternalFetcher(),
                    product_failures,
                    enrichment_failures,
                    force_refresh=True,
                )
            )

        self.assertEqual(len(products), 1)
        self.assertEqual(raw_count, 1)
        self.assertEqual(interpreted_count, 1)
        self.assertEqual(product_failures, [])
        self.assertEqual(len(returned_enrichment_failures), 1)
        self.assertEqual(returned_enrichment_failures[0]["url"], EXTERNAL_URL)
