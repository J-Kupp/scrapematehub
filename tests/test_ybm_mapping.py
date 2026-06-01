from __future__ import annotations

import unittest

from models import NormalizedProduct
from ybm import build_category_id, build_category_name_map, build_product_id, map_product_to_ybm


class YbmMappingTests(unittest.TestCase):
    def make_product(self) -> NormalizedProduct:
        return NormalizedProduct(
            product_url="https://supplier.example/products/sku-001",
            canonical_url="https://supplier.example/products/sku-001",
            category_path="Essen > Teller und Schalen > Palmblatt Bowlen",
            product_name="Palmblatt Bowl",
            item_name="Palmblatt Bowl, oval, 220 x 125 x 70 mm",
            sku="44 4297/X",
            status="ACTIVE",
            price="27.80",
            min_order_count="1",
            image_url="https://images.example/palmblatt.jpg",
            description="100% biologisch abbaubar.",
            manufacturer="Swiss Supplier",
            brand="Swissbox",
            country="CH",
            labels=["BIO", "DISCOUNTED"],
            vessel_size="1",
            vessel_unit="quantity",
            vessel_type="bowl",
            bundle_size="25",
            bundle_type="Pack",
            color="braun",
            material="Palmblatt",
            gtin="7612345678901",
        )

    def test_product_id_is_deterministic_and_url_safe(self) -> None:
        product = self.make_product()
        product_id = build_product_id(product, "swissbox")
        self.assertEqual(product_id, build_product_id(product, "swissbox"))
        self.assertTrue(product_id.startswith("swissbox__"))
        self.assertRegex(product_id, r"^[A-Za-z0-9_-]+$")

    def test_category_id_is_deterministic(self) -> None:
        category_id = build_category_id("Essen > Teller und Schalen > Palmblatt Bowlen", "swissbox")
        self.assertEqual(category_id, build_category_id("Essen > Teller und Schalen > Palmblatt Bowlen", "swissbox"))
        self.assertTrue(category_id.startswith("swissbox__cat__"))

    def test_product_mapping_builds_expected_payload(self) -> None:
        payload = map_product_to_ybm(self.make_product(), "swissbox")
        self.assertEqual(payload["status"], "ACTIVE")
        self.assertEqual(payload["price"], 2780)
        self.assertEqual(payload["category"], build_category_id("Essen > Teller und Schalen > Palmblatt Bowlen", "swissbox"))
        self.assertEqual(payload["vessel"]["unit"], "quantity")
        self.assertEqual(payload["vessel"]["type"], "BM")
        self.assertEqual(payload["bundles"][0]["type"], "PK")
        self.assertEqual(payload["custom_properties"]["country"], "CH")
        self.assertEqual(payload["custom_properties"]["labels"], ["BIO", "DISCOUNTED"])

    def test_category_name_map_shortens_long_paths(self) -> None:
        mapping = build_category_name_map(
            [
                "Deko und Ladenzubehör > Präsentation > Werbe- und Preisaufsteller",
                "Essen > Besteck und Spiesse > Apéro Spiesse",
            ]
        )
        self.assertEqual(
            mapping["Deko und Ladenzubehör > Präsentation > Werbe- und Preisaufsteller"],
            "Werbe- und Preisaufsteller",
        )
        self.assertEqual(
            mapping["Essen > Besteck und Spiesse > Apéro Spiesse"],
            "Apéro Spiesse",
        )
        self.assertLessEqual(len(mapping["Deko und Ladenzubehör > Präsentation > Werbe- und Preisaufsteller"]), 64)

    def test_category_name_map_keeps_colliding_leafs_unique(self) -> None:
        mapping = build_category_name_map(
            [
                "A > Shared",
                "B > Shared",
            ]
        )
        self.assertNotEqual(mapping["A > Shared"], mapping["B > Shared"])


if __name__ == "__main__":
    unittest.main()
