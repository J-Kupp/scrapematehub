from __future__ import annotations

import unittest

from cleaner import clean_rows
from models import CSV_COLUMNS


class CleanerTests(unittest.TestCase):
    def make_row(self, **overrides: str) -> dict[str, str]:
        row = {column: "" for column in CSV_COLUMNS}
        row.update(
            {
                "Category name": "Festivals",
                "Item ID": "36003522/05X",
                "Item name": "Cocktailserviette AirWave, 24 x 24 cm, 8 x 250 Stück / Karton",
                "Order by": "invalid",
                "Vessel size": "",
                "Vessel unit": "",
                "Vessel type": "cup",
                "Bundle size": "",
                "Bundle type": "Karton",
                "Price": "CHF 24,95/Box",
                "Price per": "",
                "Minimum order count": "1",
                "Status": "wrong",
                "Image": "https://example.com/image.png?x=1",
                "Labels": "BIO,foo,DISCOUNTED",
                "Description": "8 x 250 Stück / Karton",
                "VAT": "40.05",
            }
        )
        row.update(overrides)
        return row

    def test_clean_rows_normalizes_import_breakers(self) -> None:
        rows, corrections = clean_rows([self.make_row()])
        cleaned = rows[0]
        self.assertEqual(cleaned["Item ID"], "36003522-05X")
        self.assertEqual(cleaned["Order by"], "vessel")
        self.assertEqual(cleaned["Price per"], "vessel")
        self.assertEqual(cleaned["Status"], "ACTIVE")
        self.assertEqual(cleaned["Price"], "24.95")
        self.assertEqual(cleaned["VAT"], "")
        self.assertEqual(cleaned["Labels"], "BIO,DISCOUNTED")
        self.assertEqual(cleaned["Bundle size"], "2000")
        self.assertEqual(cleaned["Bundle type"], "CT")
        self.assertEqual(cleaned["Vessel unit"], "quantity")
        self.assertEqual(cleaned["Vessel size"], "1")
        self.assertEqual(cleaned["Vessel type"], "CU")
        self.assertTrue(any(change["field"] == "Item ID" for change in corrections))

    def test_duplicate_sanitized_item_ids_get_stable_suffix(self) -> None:
        rows, _ = clean_rows(
            [
                self.make_row(**{"Item ID": "AW402-07/8"}),
                self.make_row(**{"Item ID": "AW402-07\\8"}),
            ]
        )
        self.assertEqual(rows[0]["Item ID"], "AW402-07-8")
        self.assertEqual(rows[1]["Item ID"], "AW402-07-8-row00003")


if __name__ == "__main__":
    unittest.main()
