from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from models import SupplierConfig
from webapp.config import BootstrapUser, EcsBackendConfig, WebAppConfig
from webapp.db import connect, init_db
from webapp.jobs import queue_job
from webapp.service import (
    build_supplier_from_form,
    onboarding_status,
    resolve_allowed_artifact,
    save_dashboard_secret,
    supplier_health_summary,
    system_health,
)


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
                    "validation": {"performed": True, "errors": [], "warnings": []},
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
        self.assertEqual(summary["catalog_update_policy"], "delete_missing")
        self.assertEqual(summary["last_run_status"], "ok")
        self.assertEqual(summary["latest_job"]["job_type"], "scrape_dry_run")

    def test_supplier_form_keeps_adapter_settings_not_exposed_in_the_ui(self) -> None:
        supplier = build_supplier_from_form(
            supplier_slug="walker",
            enabled=True,
            scraper_adapter="walker",
            base_url="https://shop.walker.swiss",
            ybm_token_env_var="YBM_TOKEN_WALKER",
            output_dir="output/walker",
            catalog_update_policy="keep_existing",
            ybm_api_base="https://connect.yourbarmate.com/api",
            schedule_enabled=True,
            schedule_frequency="weekly",
            schedule_weekday="wednesday",
            schedule_time="17:39",
            concurrency="4",
            min_delay_seconds="0.1",
            max_delay_seconds="0.3",
            existing_scrape_settings={"discover_by_categories": True, "fetch_external_pages": True},
        )
        self.assertTrue(supplier.scrape_settings["discover_by_categories"])
        self.assertTrue(supplier.scrape_settings["fetch_external_pages"])
        self.assertEqual(supplier.scrape_settings["concurrency"], 4)

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
            run_summary={"validation": {"performed": True, "errors": [], "warnings": []}},
            sync_report={"aborted": False, "errors": []},
        )
        self.assertEqual(status["stage"], "Live")
        self.assertTrue(status["live"])

    def test_system_health_reports_missing_release_metadata(self) -> None:
        health = system_health(self.app_config)
        self.assertEqual(health["job_backend"], "local_subprocess")
        self.assertFalse(health["release"]["available"])
        self.assertFalse(health["ecs_runtime_status"]["enabled"])
        self.assertTrue(health["dashboard_secrets_file"].endswith("dashboard-secrets.env"))

    def test_save_dashboard_secret_writes_override_file_and_process_env(self) -> None:
        app_config = WebAppConfig(
            db_path=str(self.root / "control_panel" / "control_panel.db"),
            bootstrap_users=[BootstrapUser(username="admin", password_env_var="NOOP")],
        )
        path = save_dashboard_secret(app_config, "YBM_TOKEN_WALKER", "walker-token-123")

        self.assertEqual(path, app_config.resolved_dashboard_secrets_path())
        self.assertTrue(path.exists())
        self.assertIn("YBM_TOKEN_WALKER=walker-token-123", path.read_text(encoding="utf-8"))
        self.assertEqual(os.environ["YBM_TOKEN_WALKER"], "walker-token-123")

    def test_save_dashboard_secret_updates_aws_secrets_manager_for_worker_jobs(self) -> None:
        app_config = WebAppConfig(
            db_path=str(self.root / "control_panel" / "control_panel.db"),
            shared_secrets_backend="aws-secrets-manager",
            aws_secrets_manager_secret_id="scrapematehub-prod",
            ecs_backend=EcsBackendConfig(region="eu-central-1"),
            bootstrap_users=[BootstrapUser(username="admin", password_env_var="NOOP")],
        )
        test_case = self

        class _FakeSecretsClient:
            def __init__(self) -> None:
                self.secret_string = json.dumps({"YBM_TOKEN_SWISSBOX": "existing-token"})
                self.put_calls: list[dict[str, str]] = []

            def get_secret_value(self, **kwargs: str) -> dict[str, str]:
                test_case.assertEqual(kwargs, {"SecretId": "scrapematehub-prod"})
                return {"SecretString": self.secret_string}

            def put_secret_value(self, **kwargs: str) -> None:
                self.put_calls.append(kwargs)

        secrets_client = _FakeSecretsClient()
        fake_boto3 = ModuleType("boto3")

        def _client(service_name: str, region_name: str | None = None):
            self.assertEqual(service_name, "secretsmanager")
            self.assertEqual(region_name, "eu-central-1")
            return secrets_client

        fake_boto3.client = _client  # type: ignore[attr-defined]

        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            path = save_dashboard_secret(app_config, "YBM_TOKEN_WALKER", "walker-token-123")

        self.assertEqual(str(path), "aws-secrets-manager:/scrapematehub-prod")
        self.assertEqual(len(secrets_client.put_calls), 1)
        saved_payload = json.loads(secrets_client.put_calls[0]["SecretString"])
        self.assertEqual(saved_payload["YBM_TOKEN_SWISSBOX"], "existing-token")
        self.assertEqual(saved_payload["YBM_TOKEN_WALKER"], "walker-token-123")
        self.assertEqual(os.environ["YBM_TOKEN_WALKER"], "walker-token-123")

    def test_system_health_reports_release_and_ecs_runtime_status(self) -> None:
        control_panel_dir = self.root / "control_panel"
        control_panel_dir.mkdir(parents=True, exist_ok=True)
        release_path = control_panel_dir / "release.json"
        release_path.write_text(
            json.dumps(
                {
                    "revision": "abc123",
                    "deployed_at": "2026-06-01T12:00:00+00:00",
                    "source": "github-actions",
                    "hostname": "ip-10-0-0-1",
                }
            ),
            encoding="utf-8",
        )
        app_config = WebAppConfig(
            db_path=str(control_panel_dir / "control_panel.db"),
            job_backend="ecs_fargate",
            ecs_backend=EcsBackendConfig(
                region="eu-central-1",
                cluster="cluster",
                task_definition="family",
                container_name="worker",
                subnets=["subnet-123"],
            ),
            bootstrap_users=[BootstrapUser(username="admin", password_env_var="NOOP")],
        )

        class _FakeStsClient:
            def get_caller_identity(self) -> dict[str, str]:
                return {"Arn": "arn:aws:sts::123456789012:assumed-role/test/session"}

        class _FakeEcsClient:
            def describe_task_definition(self, taskDefinition: str) -> dict[str, object]:
                return {
                    "taskDefinition": {
                        "taskDefinitionArn": (
                            "arn:aws:ecs:eu-central-1:123456789012:"
                            f"task-definition/{taskDefinition}:1"
                        )
                    }
                }

        fake_boto3 = ModuleType("boto3")

        def _client(service_name: str, region_name: str | None = None):
            self.assertEqual(region_name, "eu-central-1")
            if service_name == "sts":
                return _FakeStsClient()
            if service_name == "ecs":
                return _FakeEcsClient()
            raise AssertionError(f"Unexpected service: {service_name}")

        fake_boto3.client = _client  # type: ignore[attr-defined]

        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            health = system_health(app_config)

        self.assertTrue(health["release"]["available"])
        self.assertEqual(health["release"]["revision"], "abc123")
        self.assertTrue(health["ecs_runtime_status"]["enabled"])
        self.assertTrue(health["ecs_runtime_status"]["credentials_ok"])
        self.assertTrue(health["ecs_runtime_status"]["task_definition_ok"])
