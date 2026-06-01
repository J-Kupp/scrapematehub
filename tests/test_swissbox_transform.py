from __future__ import annotations

import unittest

from adapters.swissbox.transform import product_candidate_from_url


class SwissboxTransformTests(unittest.TestCase):
    def test_product_candidate_from_url_accepts_detail_urls(self) -> None:
        self.assertTrue(
            product_candidate_from_url(
                "https://www.swissbox-ag.ch/detail/e524d382405349c08cff9d020d27eb54"
            )
        )

    def test_product_candidate_from_url_rejects_non_product_urls(self) -> None:
        self.assertFalse(
            product_candidate_from_url("https://www.swissbox-ag.ch/account/login"),
        )
