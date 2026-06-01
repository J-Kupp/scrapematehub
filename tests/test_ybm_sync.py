from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from models import NormalizedProduct, SupplierConfig
from ybm import sync_rows_to_ybm, sync_to_ybm


class MockHttpResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "MockHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        return None


class MockYbmBackend:
    def __init__(self) -> None:
        self.categories: dict[str, dict] = {}
        self.products: dict[str, dict] = {}
        self.auth_headers: list[str] = []

    def reset(self) -> None:
        self.categories.clear()
        self.products.clear()
        self.auth_headers.clear()

    def urlopen(self, req, timeout=None, context=None):  # type: ignore[no-untyped-def]
        del timeout, context
        self.auth_headers.append(req.headers.get("Authorization", ""))
        method = req.get_method()
        parsed = urlparse(req.full_url)
        path = parsed.path
        query = parse_qs(parsed.query)
        payload = {}
        if req.data:
            payload = json.loads(req.data.decode("utf-8"))

        if method == "GET" and path == "/categories":
            return MockHttpResponse({"categories": list(self.categories.values())})
        if method == "POST" and path == "/categories":
            self.categories[payload["id"]] = payload
            return MockHttpResponse({"categories": list(self.categories.values())})
        if method == "PUT" and path.startswith("/categories/"):
            category_id = path.rsplit("/", 1)[-1]
            self.categories[category_id] = payload
            return MockHttpResponse({"categories": list(self.categories.values())})
        if method == "GET" and path == "/products":
            products = list(self.products.values())
            cursor = int((query.get("cursor", ["0"])[0] or "0"))
            page = products[cursor : cursor + 1]
            next_cursor = str(cursor + 1) if cursor + 1 < len(products) else ""
            return MockHttpResponse({"products": page, "cursor": next_cursor})
        if method == "POST" and path == "/products":
            self.products[payload["id"]] = payload
            return MockHttpResponse(payload)
        if method == "PATCH" and path.startswith("/products/"):
            product_id = path.rsplit("/", 1)[-1]
            existing = dict(self.products.get(product_id, {"id": product_id}))
            existing.update(payload)
            self.products[product_id] = existing
            return MockHttpResponse(existing)
        raise AssertionError(f"Unhandled request: {method} {path}")


class YbmSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MockYbmBackend()
        self.urlopen_patch = patch("ybm.request.urlopen", side_effect=self.backend.urlopen)
        self.urlopen_patch.start()
        os.environ["YBM_TOKEN_TEST"] = "test-token"

    def tearDown(self) -> None:
        self.urlopen_patch.stop()

    def make_config(self) -> SupplierConfig:
        return SupplierConfig(
            supplier_slug="swissbox",
            enabled=True,
            scraper_adapter="swissbox",
            base_url="https://www.swissbox-ag.ch",
            ybm_api_base="https://mock.local",
            ybm_token_env_var="YBM_TOKEN_TEST",
            output_dir="output/swissbox",
        )

    def make_product(self, *, price: str = "10.00", sku: str = "SKU-1") -> NormalizedProduct:
        return NormalizedProduct(
            product_url=f"https://supplier.example/{sku}",
            canonical_url=f"https://supplier.example/{sku}",
            category_path="Festivals",
            product_name="Festival Cup",
            item_name="Festival Cup 300 ml",
            sku=sku,
            status="ACTIVE",
            price=price,
            min_order_count="1",
            image_url="https://images.example/cup.png",
            description="Reusable festival cup",
            brand="Supplier",
            country="CH",
            vessel_size="300",
            vessel_unit="ml",
            vessel_type="cup",
            bundle_size="20",
            bundle_type="Karton",
        )

    def make_row(self, *, price: str = "10.00", item_id: str = "SKU-1") -> dict[str, str]:
        return {
            "Category name": "Festivals",
            "Category ID": "",
            "Item ID": item_id,
            "Item name": "Festival Cup 300 ml",
            "Order by": "vessel",
            "Vessel size": "300",
            "Vessel unit": "ml",
            "Vessel type": "CU",
            "Bundle size": "20",
            "Bundle type": "CT",
            "Bundle GTIN": "",
            "Price": price,
            "Price per": "vessel",
            "Minimum order count": "1",
            "Status": "ACTIVE",
            "Image": "https://images.example/cup.png",
            "GTIN": "",
            "Labels": "",
            "Description": "Reusable festival cup",
            "Manufacturer": "",
            "Brand": "Supplier",
            "Region": "",
            "Country": "CH",
            "Vintage": "",
            "Ingredients": "",
            "Allergens": "",
            "Storage advice": "",
            "Nutritional values": "",
            "Dietary labels": "",
            "Alcohol content": "",
            "Color": "",
            "Grape variety": "",
            "Wine-making": "",
            "Material": "",
            "Fishing method": "",
            "Length": "",
            "Width": "",
            "Height": "",
            "Diameter": "",
            "Net weight": "",
            "Total weight": "",
            "VAT": "",
            "Product Sheet": "",
            "Name FR": "",
            "Name IT": "",
            "Name EN": "",
        }

    def test_sync_creates_remote_categories_and_products(self) -> None:
        summary, remote_categories, remote_products = sync_to_ybm(self.make_config(), [self.make_product()], dry_run=False)
        self.assertEqual(summary.created_categories, 1)
        self.assertEqual(summary.created_products, 1)
        self.assertEqual(len(remote_categories), 0)
        self.assertEqual(len(remote_products), 0)
        self.assertIn("token test-token", self.backend.auth_headers)
        self.assertEqual(len(self.backend.categories), 1)
        self.assertEqual(len(self.backend.products), 1)

    def test_sync_detects_unchanged_and_updates_changed_products(self) -> None:
        config = self.make_config()
        sync_to_ybm(config, [self.make_product(price="10.00")], dry_run=False)
        summary_unchanged, _, _ = sync_to_ybm(config, [self.make_product(price="10.00")], dry_run=False)
        self.assertEqual(summary_unchanged.unchanged_products, 1)
        summary_changed, _, _ = sync_to_ybm(config, [self.make_product(price="11.00")], dry_run=False)
        self.assertEqual(summary_changed.updated_products, 1)

    def test_sync_inactivates_missing_managed_products(self) -> None:
        config = self.make_config()
        sync_to_ybm(config, [self.make_product(sku="SKU-1")], dry_run=False)
        sync_to_ybm(config, [self.make_product(sku="SKU-2")], dry_run=False)
        statuses = sorted(product["status"] for product in self.backend.products.values())
        self.assertEqual(statuses, ["ACTIVE", "INACTIVE"])

    def test_row_based_sync_uses_cleaned_item_ids(self) -> None:
        config = self.make_config()
        summary, _, _ = sync_rows_to_ybm(config, [self.make_row(item_id="36003522-05X", price="24.95")], dry_run=False)
        self.assertEqual(summary.created_products, 1)
        self.assertIn("36003522-05X", self.backend.products)
        self.assertEqual(self.backend.products["36003522-05X"]["price"], 2495)

    def test_row_based_sync_reuses_existing_category_id_by_name(self) -> None:
        config = self.make_config()
        self.backend.categories["cat_existing"] = {"id": "cat_existing", "name": "Festivals"}
        summary, _, _ = sync_rows_to_ybm(config, [self.make_row(item_id="SKU-EXISTING-CAT")], dry_run=False)
        self.assertEqual(summary.created_categories, 0)
        self.assertEqual(self.backend.products["SKU-EXISTING-CAT"]["category"], "cat_existing")

    def test_row_based_sync_can_limit_products_and_skip_inactivate(self) -> None:
        config = self.make_config()
        self.backend.products["REMOTE-ONLY"] = {"id": "REMOTE-ONLY", "status": "ACTIVE", "name": "Remote only"}
        rows = [
            self.make_row(item_id="SKU-1"),
            self.make_row(item_id="SKU-2"),
        ]
        summary, _, _ = sync_rows_to_ybm(
            config,
            rows,
            dry_run=False,
            limit_products=1,
            skip_inactivate=True,
        )
        self.assertEqual(summary.created_products, 1)
        self.assertEqual(summary.inactivated_products, 0)
        self.assertIn("REMOTE-ONLY", self.backend.products)
        self.assertEqual(self.backend.products["REMOTE-ONLY"]["status"], "ACTIVE")
        self.assertEqual(len([pid for pid in self.backend.products if pid.startswith("SKU-")]), 1)


if __name__ == "__main__":
    unittest.main()
