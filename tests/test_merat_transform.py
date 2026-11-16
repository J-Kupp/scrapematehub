from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
import unittest

from adapters.merat.scraper import MeratAdapter
from adapters.merat.transform import discover_product_urls, parse_product_record
from models import SupplierConfig


FIXTURES = Path(__file__).resolve().parents[1] / "adapters" / "merat" / "fixtures"
BASE_URL = "https://shop.merat-tipesca.ch"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class MeratTransformTests(unittest.TestCase):
    def test_catalog_page_discovers_product_urls(self) -> None:
        page = load_fixture("catalog_search_page.json")
        urls = discover_product_urls(page, BASE_URL)
        self.assertEqual(len(urls), 2)
        self.assertIn(
            "https://shop.merat-tipesca.ch/portal/app/catalog/category/product/184134131110",
            urls,
        )

    def test_catalog_page_with_no_products_discovers_none(self) -> None:
        self.assertEqual(discover_product_urls({"content": []}, BASE_URL), [])

    def test_weighted_piece_preserves_fractional_kg_and_product_fields(self) -> None:
        record = load_fixture("product_184134131110.json")
        product = parse_product_record(record, BASE_URL)
        self.assertIsNotNone(product)
        assert product is not None
        self.assertEqual(product.sku, "184134131110")
        self.assertEqual(product.product_name, "Schwein Nierstück lang ca3.2kg")
        self.assertEqual(product.vessel_size, "3.2")
        self.assertEqual(product.vessel_unit, "kg")
        self.assertEqual(product.net_weight, "3.2")
        self.assertEqual(product.gtin, "7617100584656")
        self.assertEqual(product.country, "CH")
        self.assertIn("Schwein", product.category_path)
        self.assertTrue(product.image_url.startswith("https://"))

    def test_fractional_tray_weight_uses_base_ordering_unit(self) -> None:
        record = load_fixture("product_184103046700.json")
        product = parse_product_record(record, BASE_URL)
        self.assertIsNotNone(product)
        assert product is not None
        self.assertEqual(product.vessel_size, "0.4")
        self.assertEqual(product.vessel_unit, "kg")
        self.assertEqual(product.net_weight, "0.4")
        self.assertEqual(product.specs["base_ordering_unit"], "SCH")

    def test_incomplete_detail_record_is_skipped(self) -> None:
        self.assertIsNone(parse_product_record({"itemNumber": "123"}, BASE_URL))


class MeratDetailPipelineTests(unittest.TestCase):
    def pipeline(self, sku):
        from packaging import interpret_products
        from export import build_csv_rows, record_key
        from cleaner import clean_rows
        product = parse_product_record(load_fixture(f"product_{sku}.json"), BASE_URL)
        interpret_products([product], {record_key(product): sku}, [record_key(product)])
        rows, _ = clean_rows(build_csv_rows([product], "merat"), structured_item_ids={sku})
        return product, rows[0]

    def test_count_bundle_preserves_inner_weight_and_both_gtins_through_cleaning(self):
        product, row = self.pipeline("184113614060")
        self.assertEqual(row["Vessel size"], "0.06")
        self.assertEqual(row["Vessel unit"], "kg")
        self.assertEqual(row["Bundle size"], "14")
        self.assertEqual(row["Bundle type"], "CT")
        self.assertTrue(row["GTIN"])
        self.assertTrue(row["Bundle GTIN"])
        self.assertNotEqual(row["GTIN"], row["Bundle GTIN"])

    def test_optional_details_survive_export_and_sync_description(self):
        product, row = self.pipeline("184134131110")
        self.assertIn("pro 100 g", row["Nutritional values"])
        self.assertIn("559 kJ", row["Nutritional values"])
        self.assertTrue(row["Product Sheet"].startswith("https://"))
        self.assertTrue(all(row[key] for key in ["Name FR", "Name IT", "Name EN"]))
        self.assertEqual(row["Dietary labels"], "")
        self.assertIn("Suisse Garantie", row["Description"])
        self.assertIn(row["Product Sheet"], row["Description"])
        self.assertIn(row["Nutritional values"], row["Description"])
        self.assertEqual(row["Country"], "CH")
        from ybm import map_row_to_ybm
        payload = map_row_to_ybm(row, "merat")
        self.assertIn(row["Product Sheet"], payload["custom_properties"]["description"])
        self.assertIn(row["Nutritional values"], payload["custom_properties"]["description"])

    def test_fish_production_method_is_retained(self):
        product, row = self.pipeline("184191002660")
        self.assertIn("Wildfang", row["Description"])

    def test_trace_allergens_stay_distinct_from_declared_allergens(self):
        product, row = self.pipeline("184193250160")
        self.assertIn("Spurenhinweis: Sojabohnen, Hartschalenobst", row["Description"])



class MeratPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_walks_all_reported_pages(self) -> None:
        class FixtureAdapter(MeratAdapter):
            async def _request_json(self, context, url, *, data=None):
                if "list_categories" in url:
                    return [{"code": "sample"}]
                page = data["page"]
                records = [[{"itemNumber": "101", "title": "One"}], [{"itemNumber": "202", "title": "Two"}]][page]
                return {"content": records, "totalElements": 2, "totalPages": 2}

        with tempfile.TemporaryDirectory() as tmp:
            config = SupplierConfig(
                supplier_slug="merat-test",
                enabled=False,
                scraper_adapter="merat",
                base_url=BASE_URL,
                ybm_token_env_var="TEST_TOKEN",
                output_dir="output/merat-test",
                scrape_settings={"page_size": 1},
            )
            adapter = FixtureAdapter(config, Path(tmp))
            diagnostics: list[dict[str, str]] = []
            failures: list[dict[str, str]] = []
            records = await adapter._discover(object(), failures, diagnostics, logging.getLogger("merat-pagination-test"))

        self.assertEqual([record["itemNumber"] for record in records], ["101", "202"])
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
