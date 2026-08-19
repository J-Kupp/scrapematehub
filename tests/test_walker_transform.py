from __future__ import annotations

import unittest
from pathlib import Path

from adapters import ADAPTER_REGISTRY, get_adapter
from adapters.walker.scraper import robots_allows_automated_access
from adapters.walker.transform import (
    extract_german_product_urls,
    parse_product_record,
    product_candidate_from_url,
)
from config import load_supplier_configs
from webapp.service import onboarding_status, supplier_structure_status


class WalkerTransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.fixture = (cls.root / "adapters" / "walker" / "fixtures" / "product.html").read_text(encoding="utf-8")
        cls.url = "https://shop.walker.swiss/de/Alle-Produkte/Milchprodukte/Milch_-Butter-und-Eier/Rahm-und-Schlagrahm/Vollrahm-PRIMA-UHT-35_-113.html"

    def test_walker_adapter_is_registered_and_loadable(self) -> None:
        self.assertIn("walker", ADAPTER_REGISTRY)
        self.assertEqual(get_adapter("walker").__name__, "WalkerAdapter")

    def test_product_candidate_accepts_walker_detail_url(self) -> None:
        self.assertTrue(product_candidate_from_url(self.url))
        self.assertFalse(product_candidate_from_url("https://shop.walker.swiss/de/alle-produkte/milchprodukte/"))
        self.assertFalse(product_candidate_from_url("https://example.com/de/Alle-Produkte/Test-123.html"))

    def test_extract_german_product_urls_ignores_french_and_categories(self) -> None:
        xml = f"""<urlset>
        <url><loc>{self.url}</loc></url>
        <url><loc>https://shop.walker.swiss/fr/Tous-les-produits/Creme-113.html</loc></url>
        <url><loc>https://shop.walker.swiss/de/Alle-Produkte/Milchprodukte/</loc></url>
        </urlset>"""
        self.assertEqual(extract_german_product_urls(xml), {self.url})

    def test_parse_public_walker_product(self) -> None:
        product = parse_product_record(self.fixture, self.url)
        self.assertIsNotNone(product)
        assert product is not None
        self.assertEqual(product.sku, "100020")
        self.assertEqual(product.gtin, "07610900025770")
        self.assertEqual(product.item_name, "Vollrahm PRIMA UHT 35% 12 x 1 lt")
        self.assertEqual(product.category_path, "Milchprodukte > Milch, Butter und Eier > Rahm und Schlagrahm")
        self.assertEqual(product.vessel_size, "1")
        self.assertEqual(product.vessel_unit, "l")
        self.assertEqual(product.bundle_size, "12")
        self.assertEqual(product.bundle_type, "Pack")
        self.assertEqual(product.country, "CH")
        self.assertEqual(product.labels, ["Schweizer Produkt", "Vegetarisch"])
        self.assertEqual(product.price, "")
        self.assertTrue(product.image_url.endswith("/CatCache/catcache.1/pictures/113/113_M_1.jpg"))

    def test_robots_disallow_all_is_respected(self) -> None:
        self.assertFalse(robots_allows_automated_access("User-agent: *\nDisallow: *\n"))
        self.assertTrue(robots_allows_automated_access("User-agent: *\nDisallow:\n"))

    def test_walker_is_dashboard_visible_and_needs_secret(self) -> None:
        configs = {supplier.supplier_slug: supplier for supplier in load_supplier_configs()}
        self.assertIn("walker", configs)
        structure = supplier_structure_status("walker", configs["walker"].scraper_adapter)
        status = onboarding_status(
            supplier_slug="walker",
            adapter_available="walker" in ADAPTER_REGISTRY,
            structure=structure,
            secret_present=False,
            run_summary={},
            sync_report={},
        )
        self.assertEqual(status["stage"], "Needs secret")


if __name__ == "__main__":
    unittest.main()
