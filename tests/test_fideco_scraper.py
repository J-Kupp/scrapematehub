from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from adapters.fideco.scraper import Fetcher


class FidecoFetcherTests(unittest.TestCase):
    def test_fetcher_uses_http_client_and_reuses_cached_response(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text="<html>catalog</html>")

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    fetcher = Fetcher(
                        client,
                        raw_html_dir=root / "raw_html",
                        cache_dir=root / "cache",
                        min_delay_seconds=0,
                        max_delay_seconds=0,
                    )
                    first = await fetcher.fetch_text("https://fideco.ch/Shop/Sortiment/", force_refresh=True)
                    second = await fetcher.fetch_text("https://fideco.ch/Shop/Sortiment/", force_refresh=False)
            self.assertEqual(first, "<html>catalog</html>")
            self.assertEqual(second, "<html>catalog</html>")

        asyncio.run(scenario())
        self.assertEqual(len(requests), 1)
