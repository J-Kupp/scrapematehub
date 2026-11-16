from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from cleaner import clean_rows
from export import build_csv_rows
from models import CSV_COLUMNS, NormalizedProduct, SupplierConfig
from validate import validate_rows
from ybm import YbmApiError, sync_rows_to_ybm, sync_to_ybm


class MinimumOrderTests(unittest.TestCase):
    def row(self, value):
        row = dict.fromkeys(CSV_COLUMNS, "")
        row.update({"Item ID": "184101025100", "Item name": "Kalb Bratwurst",
                    "Order by": "vessel", "Vessel size": "1", "Vessel unit": "kg",
                    "Minimum order count": value, "Price per": "vessel", "Status": "ACTIVE"})
        return row

    def test_source_fraction_is_corrected_and_audited_before_api_mapping(self):
        rows, changes = clean_rows([self.row("0.001"), {**self.row("2.2"), "Item ID": "other"}])
        self.assertEqual([r["Minimum order count"] for r in rows], ["1", "3"])
        self.assertTrue(any(c["reason"] == "normalize_minimum_order_count_to_api_integer" for c in changes))
        self.assertFalse(validate_rows(rows, set(), []).errors)

    def test_invalid_minimum_is_assigned_to_its_article(self):
        for value in ["0.001", "0", "-1", "NaN", "Infinity", "invalid"]:
            with self.subTest(value=value):
                result = validate_rows([self.row(value), {**self.row("1"), "Item ID": "good"}], set(), [])
                self.assertEqual(set(result.row_errors), {"184101025100"})
                self.assertEqual(result.passed_row_count, 1)

    def test_duplicate_identity_remains_a_global_blocker(self):
        result = validate_rows([self.row("1"), self.row("1")], set(), [])
        self.assertTrue(result.errors)
        self.assertFalse(result.row_errors)
        self.assertEqual(result.passed_row_count, 0)


class PartialSyncTests(unittest.TestCase):
    def products(self):
        return [NormalizedProduct(product_url=f"https://supplier/{sku}", canonical_url=f"https://supplier/{sku}",
                                  category_path="Food", product_name=sku, item_name=sku, sku=sku,
                                  vessel_size="1", vessel_unit="kg") for sku in ["a", "b", "c"]]

    def run_sync(self, row_mode, backend):
        config = SupplierConfig("test", True, "test", "https://supplier", "TOKEN", "output")
        products = self.products()
        with patch("ybm.require_token", return_value="test-token"), patch("ybm.YbmSyncClient", return_value=backend):
            if row_mode:
                return sync_rows_to_ybm(config, build_csv_rows(products, "test"))
            return sync_to_ybm(config, products)

    def backend(self):
        backend = Mock()
        backend.list_categories.return_value = []
        backend.list_products.return_value = [{"id": "test__old", "status": "ACTIVE"}]
        return backend

    def test_article_rejection_continues_and_does_not_count_as_uploaded(self):
        for row_mode in [False, True]:
            with self.subTest(row_mode=row_mode):
                backend = self.backend()
                backend.create_product.side_effect = [None, YbmApiError("invalid article", status_code=400), None]
                summary, _, _ = self.run_sync(row_mode, backend)
                self.assertEqual(backend.create_product.call_count, 3)
                self.assertEqual(summary.created_products, 2)
                self.assertEqual(summary.failed_products, 1)
                self.assertIn("product_id=", summary.errors[0])
                self.assertTrue(summary.inactivation_skipped)
                backend.patch_product.assert_not_called()

    def test_patch_rejection_continues_with_later_articles(self):
        for row_mode in [False, True]:
            backend = self.backend()
            # Get the deterministic IDs for each API mapping mode.
            from ybm import map_product_to_ybm, map_row_to_ybm
            products = self.products()
            payloads = [map_row_to_ybm(r, "test") for r in build_csv_rows(products, "test")] if row_mode else [map_product_to_ybm(p, "test") for p in products]
            backend.list_products.return_value += [{"id": p["id"], "name": "Old name"} for p in payloads]
            backend.patch_product.side_effect = [YbmApiError("invalid patch", status_code=422), None, None]
            summary, _, _ = self.run_sync(row_mode, backend)
            self.assertEqual(backend.patch_product.call_count, 3)
            self.assertEqual(summary.updated_products, 2)
            self.assertEqual(summary.failed_products, 1)
            self.assertTrue(summary.inactivation_skipped)

    def test_auth_failure_aborts_and_preserves_completed_counts(self):
        for row_mode in [False, True]:
            backend = self.backend()
            backend.create_product.side_effect = [None, YbmApiError("unauthorized", status_code=401), None]
            with self.assertRaises(YbmApiError) as error:
                self.run_sync(row_mode, backend)
            self.assertEqual(backend.create_product.call_count, 2)
            self.assertEqual(error.exception.sync_summary["created_products"], 1)


class PartialRunIntegrationTests(unittest.TestCase):
    def test_invalid_row_is_reported_while_valid_row_is_synced(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock
        from core.contracts import SupplierScrapeResult
        from models import SyncSummary
        from orchestrator import run_supplier
        products = PartialSyncTests().products()[:2]
        products[0].min_order_count = "not-a-number"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SupplierConfig("test", True, "test", "https://supplier", "TOKEN", str(root / "output"))
            scrape = SupplierScrapeResult(products, [], {p.product_url for p in products}, [], len(products))
            with patch("orchestrator.setup_service_logger"), patch("orchestrator.load_env_file"), patch("orchestrator.get_supplier_config", return_value=config), patch("orchestrator.get_state_root", return_value=root / "state"), patch("orchestrator.scrape_supplier", new=AsyncMock(return_value=scrape)), patch("orchestrator.sync_rows_to_ybm", return_value=(SyncSummary("test", False, created_products=1), [], [])) as sync:
                with self.assertRaisesRegex(RuntimeError, "Validation failed"):
                    run_supplier("test")
            self.assertEqual([row["Item ID"] for row in sync.call_args.args[1]], ["b"])
            self.assertTrue(sync.call_args.kwargs["skip_inactivate"])
            report = json.loads((root / "output" / "test_run_summary.json").read_text())
            self.assertEqual(report["sync_summary"]["created_products"], 1)
            self.assertEqual(report["sync_summary"]["skipped_invalid_products"], 1)
            self.assertTrue(report["sync_summary"]["partial_success"])
            self.assertEqual(report["validation"]["passed_row_count"], 1)
