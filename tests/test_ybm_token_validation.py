from __future__ import annotations

import unittest
from unittest.mock import patch

from webapp.service import validate_ybm_token
from ybm import YbmApiError


class YourBarMateTokenValidationTests(unittest.TestCase):
    @patch("ybm.YbmSyncClient.list_categories", return_value=[{"id": "category-1"}])
    def test_validation_returns_visible_category_count(self, _list_categories) -> None:
        self.assertEqual(validate_ybm_token("https://api.example.test", "token-123"), 1)

    @patch(
        "ybm.YbmSyncClient.list_categories",
        side_effect=YbmApiError("GET /categories failed with 401: Unauthorized"),
    )
    def test_validation_propagates_api_auth_failure(self, _list_categories) -> None:
        with self.assertRaisesRegex(YbmApiError, "401"):
            validate_ybm_token("https://api.example.test", "bad-token")
