from __future__ import annotations

import unittest

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
