from __future__ import annotations

import json
import unittest

from adapters.gourmador.transform import (
    extract_catalog_categories,
    extract_product_links,
    parse_product_record,
    product_candidate_from_url,
)


class GourmadorTransformTests(unittest.TestCase):
    def test_product_candidate_from_url_accepts_detail_urls(self) -> None:
        self.assertTrue(
            product_candidate_from_url(
                "https://shop.gourmadorzollikofen.ch/catalog2/products/25727/2bfr-micro-leaves-amaranthroter-30g"
            )
        )

    def test_product_candidate_from_url_rejects_category_urls(self) -> None:
        self.assertFalse(
            product_candidate_from_url(
                "https://shop.gourmadorzollikofen.ch/catalog2/6352/fruchtegemuse/salate-krauter"
            )
        )

    def test_extract_catalog_categories_returns_level2_nodes(self) -> None:
        payload = {
            "component-1": {
                "constructor_type": "react_components/catalog_menu$default",
                "props": {
                    "catalog": {
                        "children": [
                            {
                                "name": "Früchte&Gemüse",
                                "tag_name": None,
                                "children": [
                                    {"catid": 6352, "name": "Salate & Kräuter", "link": "/catalog2/6352", "tag_name": None},
                                    {"catid": 6360, "name": "Frischgemüse", "link": "/catalog2/6360", "tag_name": None},
                                ],
                            },
                            {
                                "name": "Neuheiten",
                                "tag_name": "N",
                                "children": [],
                            },
                        ]
                    }
                },
            }
        }
        html = (
            '<script type="application/vnd.popscan.page-data+json" data-store="react-components">'
            f"{json.dumps(payload, ensure_ascii=False)}"
            "</script>"
        )

        categories = extract_catalog_categories(html, "https://shop.gourmadorzollikofen.ch")

        self.assertEqual(len(categories), 2)
        self.assertEqual(categories[0]["category_name"], "Salate & Kräuter")
        self.assertEqual(categories[0]["category_url"], "https://shop.gourmadorzollikofen.ch/catalog2/6352")
        self.assertEqual(categories[0]["parent_category_name"], "Früchte&Gemüse")

    def test_extract_product_links_deduplicates_title_and_image_links(self) -> None:
        html = """
        <a class="detail_img_link" href="/catalog2/products/1/first-item"></a>
        <a class="detail_link" href="/catalog2/products/1/first-item">First item</a>
        <a class="detail_link" href="/catalog2/products/2/second-item">Second item</a>
        """

        product_links = extract_product_links(html, "https://shop.gourmadorzollikofen.ch")

        self.assertEqual(
            product_links,
            [
                "https://shop.gourmadorzollikofen.ch/catalog2/products/1/first-item",
                "https://shop.gourmadorzollikofen.ch/catalog2/products/2/second-item",
            ],
        )

    def test_parse_product_record_uses_embedded_detail_payload(self) -> None:
        product = {
            "shorttext": "2BFR Micro Leaves Amaranth/Roter 30g",
            "extartnr": "3537",
            "maingroup": "Früchte&Gemüse",
            "catalog_level_2": "Salate & Kräuter",
            "catalog_level_3": "Sprossen & Kressen",
            "origin_all": {"CH": {"value": "Schweiz"}},
            "origin_key": "CH",
            "buying_currency": "CHF",
            "sv_standardprice": 1144,
            "mwst": "2.60",
            "status": 1,
            "manufacturer": "",
            "brand": "",
            "labels": [],
            "diet": "vegan, vegetarisch",
            "ingredient_list": "Amaranth rot",
            "delivered_conservation_method": "Frisch",
            "nutrition_info_energy": "227 kJ (54 kcal)",
            "nutritional_value_amount": 100,
            "nutritional_value_unit": "g",
            "nutritional_value_fat": "0.6",
            "currentdeliverysizetext": "Schale",
            "barcodes": [
                {"barcode": "3537", "quantity_as": None},
                {"barcode": "7630051901839", "quantity_as": 1},
            ],
            "product_data_sheets": [],
            "description": "",
            "detailtext": "",
            "text_generic": "",
            "extfields": {"gp_minord": "", "uinfo": {"SCH": [{"baseprice": 11.44}]}},
        }
        payload = {
            "component-1": {
                "constructor_type": "react_components/detail/detail-intro$default",
                "props": {"product": product},
            }
        }
        html = f"""
        <div class="breadcrumb">
          <ol class="breadcrumb__list">
            <li class="breadcrumb__item"><a class="breadcrumb__link" href="/catalog2">Katalog</a></li>
            <li class="breadcrumb__item"><a class="breadcrumb__link" href="/catalog2/6351">Früchte&amp;Gemüse</a></li>
            <li class="breadcrumb__item"><a class="breadcrumb__link" href="/catalog2/6352">Salate &amp; Kräuter</a></li>
            <li class="breadcrumb__item"><a class="breadcrumb__link" href="/catalog2/6358">Sprossen &amp; Kressen</a></li>
            <li class="breadcrumb__item"><span class="breadcrumb__text">2BFR Micro Leaves Amaranth/Roter 30g</span></li>
          </ol>
        </div>
        <script type="application/ld+json">{json.dumps({"image": "https://cdn.example.com/item.jpg"})}</script>
        <img class="detailpicture" src="https://cdn.example.com/item-fallback.jpg" />
        <script type="application/vnd.popscan.page-data+json" data-store="react-components">
        {json.dumps(payload, ensure_ascii=False)}
        </script>
        """

        parsed = parse_product_record(
            html,
            "https://shop.gourmadorzollikofen.ch/catalog2/products/25727/2bfr-micro-leaves-amaranthroter-30g",
        )

        assert parsed is not None
        self.assertEqual(parsed.sku, "3537")
        self.assertEqual(parsed.category_path, "Früchte&Gemüse > Salate & Kräuter > Sprossen & Kressen")
        self.assertEqual(parsed.country, "Schweiz")
        self.assertEqual(parsed.price, "11.44")
        self.assertEqual(parsed.gtin, "7630051901839")
        self.assertEqual(parsed.bundle_gtin, "7630051901839")
        self.assertEqual(parsed.image_url, "https://cdn.example.com/item.jpg")
        self.assertEqual(parsed.vessel_type, "Schale")
        self.assertEqual(parsed.labels, ["vegan", "vegetarisch"])
        self.assertEqual(parsed.specs["ingredients"], "Amaranth rot")
