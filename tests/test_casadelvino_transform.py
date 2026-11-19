from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from adapters.casadelvino import CasaDelVinoAdapter
from adapters.casadelvino.transform import detail_url, parse_product_record, parse_search_page
from cleaner import clean_rows
from export import build_csv_rows, record_key
from models import SupplierConfig
from packaging import interpret_products
from validate import validate_rows

BASE = "https://www.casadelvino.ch"
FIXTURES = Path(__file__).resolve().parents[1] / "adapters" / "casadelvino" / "fixtures"
LISTING = (FIXTURES / "listing.html").read_text()
RECORDS = json.loads((FIXTURES / "records.json").read_text())


def product(index: int):
    row = RECORDS[index]
    html = (FIXTURES / f"product_{row['artikelId']}.html").read_text()
    return parse_product_record(html, row, detail_url(row, BASE))


class CasaDelVinoDetailTests(TestCase):
    def test_listing_hydration_provides_public_product_links(self):
        result = parse_search_page(LISTING)
        self.assertEqual(result["totalRecordCount"], 3)
        self.assertEqual(detail_url(result["items"][0], BASE), BASE + "/de/shop/rotwein/estaladina-2020")
        self.assertEqual(detail_url({"urlArtikelDetail": "https://example.com/de/shop/wein/test"}, BASE), "")

    def test_detail_joins_verified_wine_information_and_separates_shop_price(self):
        wine = product(0)
        interpret_products([wine], {record_key(wine): wine.sku}, [record_key(wine)])
        rows, _ = clean_rows(build_csv_rows([wine], "casadelvino"), structured_item_ids={wine.sku})
        row = rows[0]
        self.assertEqual(row["Item ID"], "316520")
        self.assertEqual(row["Vintage"], "2020")
        self.assertEqual(row["Country"], "ES")
        self.assertEqual(row["Manufacturer"], "Mengoba")
        self.assertEqual(row["Alcohol content"], "13%")
        self.assertEqual(row["Grape variety"], "Estaladiña")
        self.assertEqual(row["Allergens"], "Sulfite")
        self.assertEqual(row["GTIN"], "8437012192157")
        self.assertEqual((row["Vessel size"], row["Vessel unit"], row["Bundle size"], row["Bundle type"]), ("75", "cl", "6", "CT"))
        self.assertIn("Handernte", row["Wine-making"])
        self.assertIn("Robert Parker: 94 / 100", row["Description"])
        self.assertIn("Charcuterie", row["Description"])
        self.assertIn("/factsheetohnepreis/", row["Product Sheet"])
        self.assertTrue(row["Image"].startswith(BASE))
        self.assertEqual(wine.specs["public_shop_price_chf"], "55")
        self.assertEqual(row["Price"], "")
        self.assertFalse(validate_rows(rows, {wine.product_url}, [], 1, [wine]).errors)

    def test_explicit_dietary_badges(self):
        wine = product(1)
        self.assertIn("BIO", wine.labels)
        self.assertEqual(wine.dietary_labels, "Vegan")

    def test_six_bottle_offer_preserves_inner_size_and_offer_price_basis(self):
        wine = product(2)
        self.assertEqual((wine.vessel_size, wine.vessel_unit, wine.bundle_size), ("75", "cl", "6"))
        self.assertEqual(wine.specs["sale_unit"], "6x 75 cl")
        self.assertEqual(wine.specs["public_shop_price_chf"], "192")
        self.assertEqual(wine.price, "")

    def test_fractional_bottle_size_survives_cleaning(self):
        wine = product(3)
        rows, _ = clean_rows(build_csv_rows([wine], "casadelvino"), structured_item_ids={wine.sku})
        self.assertEqual((rows[0]["Vessel size"], rows[0]["Vessel unit"]), ("37.5", "cl"))

    def test_rejects_detail_identity_mismatch(self):
        html = (FIXTURES / "product_30019326.html").read_text()
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_product_record(html, RECORDS[0], detail_url(RECORDS[0], BASE))


class CasaDelVinoDiscoveryTests(IsolatedAsyncioTestCase):
    async def run_adapter(self, pages):
        offsets = []

        async def response(request, url, min_delay, max_delay, data=None):
            if url.endswith("robots.txt"):
                return "User-agent: *\nDisallow: /admin/"
            if url.endswith("suchbegriff="):
                return LISTING
            if data is not None:
                offset = data["start"]
                offsets.append(offset)
                return {"totalRecordCount": 3, "items": pages[offset]}
            row = next(row for row in RECORDS if url.endswith(row["urlArtikelDetail"]))
            return (FIXTURES / f"product_{row['artikelId']}.html").read_text()

        config = SupplierConfig("casadelvino", False, "casadelvino", BASE, "TEST_TOKEN", "output/test")
        with TemporaryDirectory() as tmp, patch.object(CasaDelVinoAdapter, "_request", new=AsyncMock(side_effect=response)), patch("adapters.casadelvino.scraper.get_log_root", return_value=Path(tmp)):
            return await CasaDelVinoAdapter(config, Path(tmp)).scrape(), offsets

    async def test_discovery_advances_by_returned_count(self):
        result, offsets = await self.run_adapter({0: RECORDS[:2], 2: RECORDS[2:3]})
        self.assertEqual(offsets, [0, 2])
        self.assertEqual(len(result.discovered_product_urls), 3)
        self.assertEqual(len(result.products), 3)
        self.assertFalse(result.failures)

    async def test_incomplete_listing_reports_failure_without_partial_export(self):
        result, offsets = await self.run_adapter({0: RECORDS[:2], 2: []})
        self.assertEqual(offsets, [0, 2])
        self.assertEqual(result.failures[0]["stage"], "discovery")
        self.assertFalse(result.products)
