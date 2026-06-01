from __future__ import annotations

import unittest

from parse import product_candidate_from_url


class ParseTests(unittest.TestCase):
    def test_accepts_detail_product_urls(self) -> None:
        self.assertTrue(product_candidate_from_url("https://www.swissbox-ag.ch/detail/e524d382405349c08cff9d020d27eb54"))

    def test_accepts_single_segment_product_urls(self) -> None:
        self.assertTrue(product_candidate_from_url("https://www.swissbox-ag.ch/palmblatt-schale-rund-650-ml-oe-160-mm-h-50-mm/"))

    def test_rejects_category_urls(self) -> None:
        self.assertFalse(product_candidate_from_url("https://www.swissbox-ag.ch/essen/teller-und-schalen/"))


if __name__ == "__main__":
    unittest.main()
