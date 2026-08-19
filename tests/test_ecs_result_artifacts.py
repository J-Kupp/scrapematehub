from __future__ import annotations

import unittest

from webapp.jobs import extract_worker_run_summary


class EcsResultArtifactTests(unittest.TestCase):
    def test_extracts_final_worker_summary_from_cloudwatch_log_marker(self) -> None:
        logs = """2026-08-19 INFO scraping
2026-08-19 INFO RESULT_RUN_SUMMARY {\"product_count\":521,\"validation\":{\"performed\":true,\"errors\":[]},\"sync_summary\":{\"created_products\":521}}
"""

        self.assertEqual(
            extract_worker_run_summary(logs),
            {
                "product_count": 521,
                "validation": {"performed": True, "errors": []},
                "sync_summary": {"created_products": 521},
            },
        )

    def test_ignores_malformed_result_marker(self) -> None:
        self.assertEqual(extract_worker_run_summary("RESULT_RUN_SUMMARY not-json"), {})
