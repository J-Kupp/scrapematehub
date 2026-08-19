from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from models import SupplierConfig
from orchestrator import load_products_from_jsonl, resolve_skip_inactivate


class OrchestratorTests(unittest.TestCase):
    def test_load_products_from_jsonl_raises_clear_message_when_export_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing_products_raw.jsonl"
            with self.assertRaisesRegex(
                FileNotFoundError,
                "Run Scrape first, or use Scrape \\+ Sync",
            ):
                load_products_from_jsonl(missing)

    def test_resolve_skip_inactivate_defaults_from_catalog_policy(self) -> None:
        self.assertFalse(
            resolve_skip_inactivate(
                SupplierConfig(
                    supplier_slug="demo",
                    enabled=True,
                    scraper_adapter="swissbox",
                    base_url="https://example.com",
                    ybm_token_env_var="YBM_TOKEN_DEMO",
                    output_dir="output/demo",
                    catalog_update_policy="delete_missing",
                )
            )
        )
        self.assertTrue(
            resolve_skip_inactivate(
                SupplierConfig(
                    supplier_slug="demo",
                    enabled=True,
                    scraper_adapter="swissbox",
                    base_url="https://example.com",
                    ybm_token_env_var="YBM_TOKEN_DEMO",
                    output_dir="output/demo",
                    catalog_update_policy="keep_existing",
                )
            )
        )
