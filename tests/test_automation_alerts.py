from __future__ import annotations

import unittest

from webapp.alerts import automation_alert_reason


class AutomationAlertTests(unittest.TestCase):
    def test_only_scheduled_jobs_can_trigger_alerts(self) -> None:
        reason = automation_alert_reason(
            {"requested_by": "admin"},
            status="failed",
            error_message="boom",
            run_summary={},
            alert_settings={},
        )
        self.assertEqual(reason, "")

    def test_failed_scheduled_job_alerts_immediately(self) -> None:
        reason = automation_alert_reason(
            {"requested_by": "scheduler"},
            status="failed",
            error_message="Worker exited unexpectedly.",
            run_summary={},
            alert_settings={},
        )
        self.assertIn("Worker exited", reason)

    def test_partial_failures_require_both_thresholds(self) -> None:
        summary = {"stages": {"scrape": {"failure_count": 10, "raw_record_count": 100}}}
        settings = {"minimum_failures": 10, "failure_rate_percent": 5}
        self.assertIn(
            "10 product errors",
            automation_alert_reason(
                {"requested_by": "scheduler"},
                status="succeeded",
                error_message="",
                run_summary=summary,
                alert_settings=settings,
            ),
        )
        summary["stages"]["scrape"]["failure_count"] = 4
        self.assertEqual(
            automation_alert_reason(
                {"requested_by": "scheduler"},
                status="succeeded",
                error_message="",
                run_summary=summary,
                alert_settings=settings,
            ),
            "",
        )
