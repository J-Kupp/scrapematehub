from __future__ import annotations

import unittest
from pathlib import Path

from adapters.mundo.transform import (
    extract_next_listing_url,
    extract_product_links,
    extract_root_category_links,
    parse_product_record,
)


FIXTURES = Path(__file__).resolve().parents[1] / "adapters" / "mundo" / "fixtures"


class MundoTransformTests(unittest.TestCase):
    def test_listing_fixture_discovers_categories_products_and_next_page(self) -> None:
        html = (FIXTURES / "listing.html").read_text(encoding="utf-8")
        base_url = "https://www.mundoag.ch"
        categories = extract_root_category_links(html, base_url)
        products = extract_product_links(html, base_url)

        self.assertEqual(len(categories), 2)
        self.assertEqual(len(products), 2)
        self.assertEqual(
            extract_next_listing_url(html, base_url),
            "https://www.mundoag.ch/catalog2/3163/convenience?page=2",
        )

    def test_detail_fixture_maps_public_product_data(self) -> None:
        html = (FIXTURES / "detail.html").read_text(encoding="utf-8")
        product = parse_product_record(
            html,
            "https://www.mundoag.ch/catalog2/products/6248/convenience/saucen/salatsaucen/salatsauce-tomate-basilikum",
        )

        assert product is not None
        self.assertEqual(product.sku, "43625")
        self.assertEqual(product.category_path, "Convenience > Saucen > Salatsaucen")
        self.assertEqual(product.image_url, "https://www.psassets.ch/thumbs/salatsauce.jpg")
        self.assertEqual(product.vessel_size, "5")
        self.assertEqual(product.vessel_unit, "dl")
        self.assertEqual(product.bundle_size, "6")
        self.assertEqual(product.bundle_type, "pack")
        self.assertEqual(product.ingredients, "Sonnenblumenöl, Tomaten, Basilikum")
        self.assertEqual(product.allergens, "egg, mustard, soy")
        self.assertEqual(product.storage_advice, "Gekühlt (0-5º)")
        self.assertIn("Kalorien: 272", product.nutritional_values)
        self.assertIn("vegetarisch", product.dietary_labels)
        self.assertEqual(product.product_sheet_url, "https://login.trustbox-swiss.ch/product/43625")
