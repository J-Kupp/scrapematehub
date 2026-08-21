from __future__ import annotations

import unittest
from pathlib import Path

from adapters import ADAPTER_REGISTRY, adapter_path_info


class AdapterProgressContractTests(unittest.TestCase):
    def test_every_registered_adapter_emits_standard_live_progress_markers(self) -> None:
        for adapter_name in ADAPTER_REGISTRY:
            scraper_path = Path(adapter_path_info(adapter_name)["scraper_path"])
            source = scraper_path.read_text(encoding="utf-8")
            with self.subTest(adapter=adapter_name):
                self.assertIn("PROGRESS phase=discovering", source)
                self.assertIn("PROGRESS phase=processing", source)

    def test_every_registered_adapter_has_supplier_scoped_tests(self) -> None:
        root = Path(__file__).resolve().parent
        for adapter_name in ADAPTER_REGISTRY:
            with self.subTest(adapter=adapter_name):
                self.assertTrue((root / f"test_{adapter_name}_transform.py").exists())
