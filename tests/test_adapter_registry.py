from __future__ import annotations

import unittest
from pathlib import Path

from adapters import ADAPTER_REGISTRY, adapter_path_info, get_adapter


class AdapterRegistryTests(unittest.TestCase):
    def test_every_registered_adapter_is_loadable(self) -> None:
        for adapter_name in ADAPTER_REGISTRY:
            with self.subTest(adapter=adapter_name):
                adapter_cls = get_adapter(adapter_name)
                self.assertTrue(adapter_cls.__name__.endswith("Adapter"))

    def test_adapter_path_info_points_to_package_structure(self) -> None:
        for adapter_name in ADAPTER_REGISTRY:
            with self.subTest(adapter=adapter_name):
                info = adapter_path_info(adapter_name)
                self.assertTrue(Path(info["scraper_path"]).exists())
                self.assertTrue(Path(info["transformer_path"]).exists())
                self.assertTrue(Path(info["fixtures_path"]).exists())
                self.assertTrue(info["tests_hint"].endswith(f"test_{adapter_name}_transform.py"))
