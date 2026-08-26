from __future__ import annotations

import unittest

from models import CSV_COLUMNS
from validate import validate_rows


class ValidationTests(unittest.TestCase):
    def test_discovered_urls_with_no_exported_rows_blocks_sync(self) -> None:
        result = validate_rows(
            rows=[],
            discovered_product_urls={"https://supplier.example/product"},
            failures=[{"stage": "transform", "reason": "missing title"}],
        )
        self.assertFalse(result.is_valid)
        self.assertIn("refusing an empty catalog sync", result.errors[0])

    def test_vessel_size_precision_is_validated_before_sync(self) -> None:
        row = {column: "" for column in CSV_COLUMNS}
        row.update({"Item ID": "saffron", "Order by": "vessel", "Vessel size": "0.0005", "Vessel unit": "kg", "Price per": "vessel", "Status": "ACTIVE"})
        result = validate_rows(rows=[row], discovered_product_urls=set(), failures=[])
        self.assertFalse(result.is_valid)
        self.assertIn("Invalid Vessel size for saffron", result.errors[0])
