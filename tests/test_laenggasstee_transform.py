from __future__ import annotations

import unittest

from adapters.laenggasstee.scraper import extract_storefront_config
from adapters.laenggasstee.transform import parse_product_record, product_url


class LaenggassTeeTransformTests(unittest.TestCase):
    def test_extract_storefront_config_reads_public_shopware_settings(self) -> None:
        html = '''SHOP_API_URL:"https://shop.example.ch/store-api/",accessToken:"public-key",x="product-listing-01914fa5c56870658ce7cc827a99c5d8"'''
        self.assertEqual(
            extract_storefront_config(html),
            (
                "https://shop.example.ch/store-api",
                "public-key",
                "01914fa5c56870658ce7cc827a99c5d8",
            ),
        )

    def test_parse_product_record_maps_shopware_tea_data(self) -> None:
        raw = {
            "id": "01921fb17a1c70f3ad28664aec30dc75",
            "productNumber": "4TAL",
            "name": "A Li Shan",
            "translated": {"description": "<p>A delicate oolong.</p>", "packUnit": "50g"},
            "active": True,
            "available": True,
            "calculatedPrice": {"unitPrice": 31.5},
            "tax": {"taxRate": 2.6},
            "cover": {"media": {"url": "https://images.example/a-li-shan.jpg"}},
            "categories": [{"translated": {"breadcrumb": ["Länggass-Tee", "Camellia sinensis", "Oolong Tee"]}}],
            "properties": [{"translated": {"name": "Qing Xin Wu Long"}}],
            "weight": 0.07,
            "stock": 9999,
            "extensions": {"teaProductExtension": {"googleLink": "Taiwan / Jiayi", "harvestDate": "Oktober 2023"}},
        }
        parsed = parse_product_record(raw, "https://laenggasstee.ch")

        assert parsed is not None
        self.assertEqual(parsed.sku, "4TAL")
        self.assertEqual(parsed.price, "31.5")
        self.assertEqual(parsed.vat, "2.6")
        self.assertEqual(parsed.vessel_size, "50")
        self.assertEqual(parsed.vessel_unit, "g")
        self.assertEqual(parsed.category_path, "Camellia sinensis > Oolong Tee")
        self.assertEqual(parsed.country, "Taiwan")
        self.assertEqual(parsed.image_url, "https://images.example/a-li-shan.jpg")
        self.assertEqual(parsed.labels, ["Qing Xin Wu Long"])
        self.assertEqual(parsed.specs["tea_harvestDate"], "Oktober 2023")
        self.assertEqual(parsed.canonical_url, product_url("https://laenggasstee.ch", raw["id"]))
