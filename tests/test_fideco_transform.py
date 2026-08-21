from __future__ import annotations

import unittest
from pathlib import Path

from adapters.fideco.transform import (
    extract_category_links,
    extract_listing_page_count,
    extract_product_links,
    listing_page_url,
    parse_product_record,
)


FIXTURES = Path(__file__).resolve().parents[1] / "adapters" / "fideco" / "fixtures"


class FidecoTransformTests(unittest.TestCase):
    def test_listing_fixture_discovers_products_categories_and_all_pages(self) -> None:
        html = (FIXTURES / "listing.html").read_text(encoding="utf-8")
        self.assertEqual(extract_product_links(html, "https://fideco.ch"), ["https://fideco.ch/Suppe/ABC-42", "https://fideco.ch/Teriyaki-Sauce/18071"])
        self.assertEqual(extract_category_links(html, "https://fideco.ch"), ["https://fideco.ch/Shop/Fisch-Meeresfruechte/", "https://fideco.ch/Shop/Fisch-Meeresfruechte/Frischfisch/"])
        self.assertEqual(extract_listing_page_count(html), 3)
        self.assertEqual(listing_page_url("https://fideco.ch/Shop/Sortiment/", 2), "https://fideco.ch/Shop/Sortiment/?p=2")

    def test_product_fixture_transforms_public_detail_data(self) -> None:
        product = parse_product_record((FIXTURES / "product.html").read_text(encoding="utf-8"), "https://fideco.ch/Teriyaki-Sauce/18071")
        assert product is not None
        self.assertEqual(product.sku, "18071")
        self.assertEqual(product.category_path, "Sortiment > Asia & Orient")
        self.assertEqual(product.image_url, "https://fideco.ch/media/teriyaki.jpg")
        self.assertEqual(product.vessel_size, "2")
        self.assertEqual(product.vessel_unit, "kg")
        self.assertEqual(product.vessel_type, "Flasche")
        self.assertEqual(product.specs["allergene"], "Soja")
