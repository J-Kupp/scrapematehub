from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from models import SupplierConfig
from webapp.config import BootstrapUser, WebAppConfig
from webapp.db import connect, init_db
from webapp.jobs import queue_job
from webapp.service import onboarding_status, resolve_allowed_artifact, supplier_health_summary


class WebAppServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.conn = connect(self.root / "control_panel.db")
        init_db(self.conn)
        self.supplier = SupplierConfig(
            supplier_slug="swissbox",
            enabled=True,
            scraper_adapter="swissbox",
            base_url="https://www.swissbox-ag.ch",
            ybm_token_env_var="YBM_TOKEN_SWISSBOX",
            output_dir="output/swissbox",
            schedule={"frequency": "weekly", "weekday": "monday", "time": "03:30"},
        )
        self.app_config = WebAppConfig(
            db_path=str(self.root / "control_panel.db"),
            allowed_artifact_roots=["output", "logs", "state"],
            bootstrap_users=[BootstrapUser(username="admin", password_env_var="NOOP")],
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_supplier_health_summary_combines_config_state_and_artifacts(self) -> None:
        output_dir = self.root / "output" / "swissbox"
        state_dir = self.root / "state" / "swissbox"
        logs_dir = self.root / "logs"
        output_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        run_summary_path = output_dir / "swissbox_run_summary.json"
        sync_report_path = output_dir / "swissbox_sync_report.json"
        state_file_path = state_dir / "state.json"
        log_path = logs_dir / "service.log"

        run_summary_path.write_text(
            json.dumps(
                {
                    "product_count": 2419,
                    "validation": {"errors": [], "warnings": []},
                }
            ),
            encoding="utf-8",
        )
        sync_report_path.write_text(
            json.dumps({"updated_products": 10, "created_products": 2}),
            encoding="utf-8",
        )
        state_file_path.write_text(
            json.dumps({"supplier_slug": "swissbox", "last_successful_run_at": "2026-05-28T10:00:00+00:00"}),
            encoding="utf-8",
        )
        log_path.write_text("service ok\n", encoding="utf-8")

        queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_dry_run",
            requested_by="admin",
            env_file_ref=".env.local",
            command=["python", "scraper.py", "dry-run-supplier", "swissbox"],
        )
        os.environ["YBM_TOKEN_SWISSBOX"] = "present"

        fake_paths = {
            "output_dir": output_dir,
            "csv": output_dir / "swissbox_products.csv",
            "correction_report": output_dir / "swissbox_corrections.csv",
            "packaging_audit": output_dir / "swissbox_packaging_audit.csv",
            "raw_jsonl": output_dir / "swissbox_products_raw.jsonl",
            "failures_jsonl": output_dir / "swissbox_products_failures.jsonl",
            "listing_diagnostics": output_dir / "swissbox_listing_diagnostics.jsonl",
            "run_summary": run_summary_path,
            "sync_report": sync_report_path,
            "state_file": state_file_path,
            "remote_products_snapshot": state_dir / "remote_products.json",
            "remote_categories_snapshot": state_dir / "remote_categories.json",
        }

        with patch("webapp.service.load_supplier_configs", return_value=[self.supplier]), patch(
            "webapp.service.supplier_paths",
            return_value=fake_paths,
        ):
            summaries = supplier_health_summary(self.conn, self.app_config)

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["supplier_slug"], "swissbox")
        self.assertTrue(summary["secret_present"])
        self.assertEqual(summary["product_count"], 2419)
        self.assertEqual(summary["last_run_status"], "ok")
        self.assertEqual(summary["latest_job"]["job_type"], "scrape_dry_run")

    def test_resolve_allowed_artifact_allows_known_roots_only(self) -> None:
        app_config = WebAppConfig(
            db_path=str(self.root / "control_panel.db"),
            allowed_artifact_roots=[str(self.root / "output"), str(self.root / "logs")],
        )
        allowed = self.root / "output" / "swissbox_products.csv"
        allowed.parent.mkdir(parents=True, exist_ok=True)
        allowed.write_text("x", encoding="utf-8")

        resolved = resolve_allowed_artifact(str(allowed), app_config)
        self.assertEqual(resolved, allowed.resolve())

        blocked = self.root / "private" / "secret.txt"
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("nope", encoding="utf-8")
        with self.assertRaises(PermissionError):
            resolve_allowed_artifact(str(blocked), app_config)

    def test_onboarding_status_handles_config_only_supplier(self) -> None:
        structure = {
            "scraper_exists": False,
            "transformer_exists": False,
            "fixtures_present": False,
            "tests_present": False,
        }
        status = onboarding_status(
            supplier_slug="future_vendor",
            adapter_available=False,
            structure=structure,
            secret_present=False,
            run_summary={},
            sync_report={},
        )
        self.assertEqual(status["stage"], "Config only")
        self.assertFalse(status["live"])

    def test_onboarding_status_marks_live_after_validation_and_sync(self) -> None:
        structure = {
            "scraper_exists": True,
            "transformer_exists": True,
            "fixtures_present": True,
            "tests_present": True,
        }
        status = onboarding_status(
            supplier_slug="swissbox",
            adapter_available=True,
            structure=structure,
            secret_present=True,
            run_summary={"validation": {"errors": [], "warnings": []}},
            sync_report={"aborted": False, "errors": []},
        )
        self.assertEqual(status["stage"], "Live")
        self.assertTrue(status["live"])
