from __future__ import annotations

import unittest
from pathlib import Path

from adapters import ADAPTER_REGISTRY, adapter_path_info, get_adapter


class AdapterRegistryTests(unittest.TestCase):
    def test_swissbox_adapter_is_registered_and_loadable(self) -> None:
        self.assertIn("swissbox", ADAPTER_REGISTRY)
        adapter_cls = get_adapter("swissbox")
        self.assertEqual(adapter_cls.__name__, "SwissboxAdapter")

    def test_gourmador_adapter_is_registered_and_loadable(self) -> None:
        self.assertIn("gourmador", ADAPTER_REGISTRY)
        adapter_cls = get_adapter("gourmador")
        self.assertEqual(adapter_cls.__name__, "GourmadorAdapter")

    def test_terravigna_adapter_is_registered_and_loadable(self) -> None:
        self.assertIn("terravigna", ADAPTER_REGISTRY)
        adapter_cls = get_adapter("terravigna")
        self.assertEqual(adapter_cls.__name__, "TerraVignaAdapter")

    def test_adapter_path_info_points_to_package_structure(self) -> None:
        info = adapter_path_info("swissbox")
        self.assertTrue(Path(info["scraper_path"]).exists())
        self.assertTrue(Path(info["transformer_path"]).exists())
        self.assertTrue(Path(info["fixtures_path"]).exists())
        self.assertTrue(info["tests_hint"].endswith("test_swissbox_transform.py"))
