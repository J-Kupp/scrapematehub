from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp.app import create_app, summarize_job_logs


class WebAppAppTests(unittest.TestCase):
    def test_summarize_job_logs_counts_items_and_surfaces_errors(self) -> None:
        summary = summarize_job_logs(
            "\n".join(
                [
                    "2026-06-04 10:17:36,983 INFO Parsed product 1 Baum sku=SB1",
                    "2026-06-04 10:17:37,393 INFO Parsed product 2 Baeume sku=SB2",
                    "2026-06-04 10:17:38,001 ERROR Failed to parse variant price",
                    "Traceback (most recent call last):",
                ]
            )
        )
        self.assertIn("Scraped items: 2", summary)
        self.assertIn("Potential errors:", summary)
        self.assertIn("ERROR Failed to parse variant price", summary)
        self.assertIn("Traceback", summary)

    def test_login_and_protected_api_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "control_panel.db"
            env_path = root / ".env.local"
            config_path = root / "control_panel.json"
            supplier_config_path = root / "suppliers.json"
            env_path.write_text("", encoding="utf-8")
            os.environ["CONTROL_PANEL_ADMIN_PASSWORD"] = "admin-pass"
            os.environ["CONTROL_PANEL_SESSION_SECRET"] = "session-secret-for-tests"
            config_path.write_text(
                json.dumps(
                    {
                        "db_path": str(db_path),
                        "supplier_config_path": str(supplier_config_path),
                        "env_file": str(env_path),
                        "scheduler_enabled": False,
                        "allowed_artifact_roots": ["output", "logs", "state"],
                        "bootstrap_users": [
                            {
                                "username": "admin",
                                "password_env_var": "CONTROL_PANEL_ADMIN_PASSWORD",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            app = create_app(config_path)
            with TestClient(app) as client:
                response = client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn("Sign in", response.text)

                login_response = client.post(
                    "/login",
                    data={"username": "admin", "password": "admin-pass"},
                    follow_redirects=True,
                )
                self.assertEqual(login_response.status_code, 200)
                self.assertIn("Supplier Control Panel", login_response.text)

                suppliers_response = client.get("/api/suppliers")
                self.assertEqual(suppliers_response.status_code, 200)
                self.assertIsInstance(suppliers_response.json(), list)

                public_health_response = client.get("/healthz")
                self.assertEqual(public_health_response.status_code, 200)
                self.assertEqual(public_health_response.json()["status"], "ok")

                settings_response = client.get("/settings")
                self.assertEqual(settings_response.status_code, 200)
                self.assertIn("Change Password", settings_response.text)

                connections_response = client.get("/connections")
                self.assertEqual(connections_response.status_code, 200)
                self.assertIn("Connections & Onboarding", connections_response.text)

                password_response = client.post(
                    "/settings/password",
                    data={
                        "current_password": "admin-pass",
                        "new_password": "new-admin-pass",
                        "confirm_password": "new-admin-pass",
                    },
                    follow_redirects=True,
                )
                self.assertEqual(password_response.status_code, 200)
                self.assertIn("Password updated successfully.", password_response.text)

                new_supplier_response = client.post(
                    "/suppliers/new",
                    data={
                        "supplier_slug": "demo",
                        "enabled": "on",
                        "scraper_adapter": "swissbox",
                        "base_url": "https://example.com",
                        "ybm_token_env_var": "YBM_TOKEN_DEMO",
                        "output_dir": "output/demo",
                        "ybm_api_base": "https://connect.yourbarmate.com/api",
                        "schedule_enabled": "on",
                        "schedule_frequency": "weekly",
                        "schedule_weekday": "friday",
                        "schedule_time": "04:15",
                        "concurrency": "1",
                        "min_delay_seconds": "0.10",
                        "max_delay_seconds": "0.30",
                    },
                    follow_redirects=True,
                )
                self.assertEqual(new_supplier_response.status_code, 200)
                self.assertIn("Supplier created.", new_supplier_response.text)
                self.assertIn("demo", new_supplier_response.text)

                api_supplier_response = client.get("/api/suppliers/demo")
                self.assertEqual(api_supplier_response.status_code, 200)
                self.assertEqual(
                    api_supplier_response.json()["onboarding"]["stage"],
                    "Config only",
                )

                scrape_response = client.post("/api/suppliers/demo/jobs/scrape")
                self.assertEqual(scrape_response.status_code, 410)
                self.assertIn("removed", scrape_response.json()["detail"])

                sync_response = client.post("/api/suppliers/demo/jobs/sync-from-export")
                self.assertEqual(sync_response.status_code, 410)
                self.assertIn("removed", sync_response.json()["detail"])

                missing_response = client.post("/api/suppliers/nope/jobs/dry-run")
                self.assertEqual(missing_response.status_code, 404)

    def test_job_detail_shows_stop_button_for_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "control_panel.db"
            env_path = root / ".env.local"
            config_path = root / "control_panel.json"
            supplier_config_path = root / "suppliers.json"
            env_path.write_text("", encoding="utf-8")
            os.environ["CONTROL_PANEL_ADMIN_PASSWORD"] = "admin-pass"
            os.environ["CONTROL_PANEL_SESSION_SECRET"] = "session-secret-for-tests"
            config_path.write_text(
                json.dumps(
                    {
                        "db_path": str(db_path),
                        "supplier_config_path": str(supplier_config_path),
                        "env_file": str(env_path),
                        "scheduler_enabled": False,
                        "allowed_artifact_roots": ["output", "logs", "state"],
                        "bootstrap_users": [
                            {
                                "username": "admin",
                                "password_env_var": "CONTROL_PANEL_ADMIN_PASSWORD",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            app = create_app(config_path)
            conn = app.state.db

            with TestClient(app) as client:
                client.post(
                    "/login",
                    data={"username": "admin", "password": "admin-pass"},
                    follow_redirects=True,
                )
                job_id = conn.execute(
                    """
                    INSERT INTO jobs (
                        supplier_slug, job_type, status, requested_by, requested_at,
                        started_at, command, env_file_ref, backend
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "swissbox",
                        "scrape_dry_run",
                        "running",
                        "admin",
                        "2026-06-04T12:00:00+00:00",
                        "2026-06-04T12:00:10+00:00",
                        json.dumps(["python", "scraper.py", "dry-run-supplier", "swissbox"]),
                        str(env_path),
                        "ecs_fargate",
                    ),
                ).lastrowid
                conn.commit()
                job_response = client.get(f"/jobs/{job_id}")
                self.assertEqual(job_response.status_code, 200)
                self.assertIn("Stop Job", job_response.text)

    def test_job_detail_shows_stage_summary_for_scrape_and_sync_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "control_panel.db"
            env_path = root / ".env.local"
            config_path = root / "control_panel.json"
            supplier_config_path = root / "suppliers.json"
            output_dir = root / "output" / "swissbox"
            output_dir.mkdir(parents=True, exist_ok=True)
            env_path.write_text("", encoding="utf-8")
            os.environ["CONTROL_PANEL_ADMIN_PASSWORD"] = "admin-pass"
            os.environ["CONTROL_PANEL_SESSION_SECRET"] = "session-secret-for-tests"
            config_path.write_text(
                json.dumps(
                    {
                        "db_path": str(db_path),
                        "supplier_config_path": str(supplier_config_path),
                        "env_file": str(env_path),
                        "scheduler_enabled": False,
                        "allowed_artifact_roots": ["output", "logs", "state"],
                        "bootstrap_users": [
                            {
                                "username": "admin",
                                "password_env_var": "CONTROL_PANEL_ADMIN_PASSWORD",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            run_summary_path = output_dir / "swissbox_run_summary.json"
            sync_report_path = output_dir / "swissbox_sync_report.json"
            run_summary_path.write_text(
                json.dumps(
                    {
                        "mode": "scrape_and_sync",
                        "stages": {
                            "scrape": {
                                "product_count": 473,
                                "raw_record_count": 473,
                                "interpreted_record_count": 473,
                                "failure_count": 3,
                                "covered_product_url_count": 476,
                                "discovered_product_url_count": 476,
                            },
                            "validation": {
                                "performed": True,
                                "row_count": 470,
                                "passed_row_count": 470,
                                "warning_count": 4,
                                "error_count": 0,
                                "errors": [],
                            },
                            "sync": {
                                "performed": True,
                                "old_catalog_products": 470,
                                "uploaded_products": 41,
                                "created_products": 11,
                                "updated_products": 30,
                                "unchanged_products": 429,
                                "errors": [],
                                "aborted": False,
                                "dry_run": False,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            sync_report_path.write_text(
                json.dumps({"created_products": 11, "updated_products": 30, "errors": []}),
                encoding="utf-8",
            )

            app = create_app(config_path)
            conn = app.state.db

            with TestClient(app) as client:
                client.post(
                    "/login",
                    data={"username": "admin", "password": "admin-pass"},
                    follow_redirects=True,
                )
                job_id = conn.execute(
                    """
                    INSERT INTO jobs (
                        supplier_slug, job_type, status, requested_by, requested_at,
                        started_at, command, env_file_ref, backend, run_summary_path, sync_report_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "swissbox",
                        "scrape_and_sync",
                        "succeeded",
                        "admin",
                        "2026-06-04T12:00:00+00:00",
                        "2026-06-04T12:00:10+00:00",
                        json.dumps(["python", "scraper.py", "run-supplier", "swissbox"]),
                        str(env_path),
                        "ecs_fargate",
                        str(run_summary_path),
                        str(sync_report_path),
                    ),
                ).lastrowid
                conn.commit()
                job_response = client.get(f"/jobs/{job_id}")
                self.assertEqual(job_response.status_code, 200)
                self.assertIn("Stage Summary", job_response.text)
                self.assertIn("Passed validation", job_response.text)
                self.assertIn("Items uploaded", job_response.text)
                self.assertIn("470 checked", job_response.text)
                self.assertIn("470 old in catalog", job_response.text)
                self.assertIn("11 created", job_response.text)

    def test_stop_job_route_redirects_after_requesting_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "control_panel.db"
            env_path = root / ".env.local"
            config_path = root / "control_panel.json"
            supplier_config_path = root / "suppliers.json"
            env_path.write_text("", encoding="utf-8")
            os.environ["CONTROL_PANEL_ADMIN_PASSWORD"] = "admin-pass"
            os.environ["CONTROL_PANEL_SESSION_SECRET"] = "session-secret-for-tests"
            config_path.write_text(
                json.dumps(
                    {
                        "db_path": str(db_path),
                        "supplier_config_path": str(supplier_config_path),
                        "env_file": str(env_path),
                        "scheduler_enabled": False,
                        "allowed_artifact_roots": ["output", "logs", "state"],
                        "bootstrap_users": [
                            {
                                "username": "admin",
                                "password_env_var": "CONTROL_PANEL_ADMIN_PASSWORD",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            app = create_app(config_path)
            conn = app.state.db
            job_id = conn.execute(
                """
                INSERT INTO jobs (
                    supplier_slug, job_type, status, requested_by, requested_at,
                    started_at, command, env_file_ref, backend
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "swissbox",
                    "scrape_dry_run",
                    "running",
                    "admin",
                    "2026-06-04T12:00:00+00:00",
                    "2026-06-04T12:00:10+00:00",
                    json.dumps(["python", "scraper.py", "dry-run-supplier", "swissbox"]),
                    str(env_path),
                    "ecs_fargate",
                ),
            ).lastrowid
            conn.commit()

            with TestClient(app) as client:
                client.post(
                    "/login",
                    data={"username": "admin", "password": "admin-pass"},
                    follow_redirects=True,
                )
                with patch.object(app.state.job_runner, "stop_job", return_value={"id": job_id}):
                    response = client.post(f"/jobs/{job_id}/stop", follow_redirects=False)
                self.assertEqual(response.status_code, 302)
                self.assertIn(f"/jobs/{job_id}?notice=Stop+requested.", response.headers["location"])

    def test_ecs_job_logs_endpoint_reads_cloudwatch_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "control_panel.db"
            env_path = root / ".env.local"
            config_path = root / "control_panel.json"
            supplier_config_path = root / "suppliers.json"
            env_path.write_text("", encoding="utf-8")
            os.environ["CONTROL_PANEL_ADMIN_PASSWORD"] = "admin-pass"
            os.environ["CONTROL_PANEL_SESSION_SECRET"] = "session-secret-for-tests"
            config_path.write_text(
                json.dumps(
                    {
                        "db_path": str(db_path),
                        "supplier_config_path": str(supplier_config_path),
                        "env_file": str(env_path),
                        "scheduler_enabled": False,
                        "job_backend": "ecs_fargate",
                        "ecs_backend": {
                            "region": "eu-central-1",
                            "cluster": "cluster",
                            "task_definition": "family",
                            "container_name": "scraper-worker",
                            "subnets": ["subnet-123"],
                        },
                        "allowed_artifact_roots": ["output", "logs", "state"],
                        "bootstrap_users": [
                            {
                                "username": "admin",
                                "password_env_var": "CONTROL_PANEL_ADMIN_PASSWORD",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            app = create_app(config_path)
            conn = app.state.db
            job_id = conn.execute(
                """
                INSERT INTO jobs (
                    supplier_slug, job_type, status, requested_by, requested_at, finished_at,
                    command, env_file_ref, backend, remote_job_id, remote_status,
                    cloudwatch_log_group, cloudwatch_log_stream
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "swissbox",
                    "scrape_dry_run",
                    "running",
                    "admin",
                    "2026-06-04T12:00:00+00:00",
                    None,
                    json.dumps(["python", "scraper.py", "dry-run-supplier", "swissbox"]),
                    str(env_path),
                    "ecs_fargate",
                    "arn:aws:ecs:eu-central-1:123456789012:task/cluster/task-123",
                    "RUNNING",
                    "/ecs/yourbarmate-scraper-worker",
                    "ecs/scraper-worker/task-123",
                ),
            ).lastrowid
            conn.commit()

            with TestClient(app) as client:
                client.post(
                    "/login",
                    data={"username": "admin", "password": "admin-pass"},
                    follow_redirects=True,
                )
                with patch(
                    "webapp.app.read_ecs_task_logs",
                    return_value=(
                        "/ecs/yourbarmate-scraper-worker",
                        "ecs/scraper-worker/task-123",
                        "\n".join(
                            [
                                "[2026-06-04T12:00:00+00:00] INFO Swissbox scrape started.",
                                "[2026-06-04T12:00:01+00:00] INFO Parsed product One sku=1",
                                "[2026-06-04T12:00:02+00:00] INFO Parsed product Two sku=2",
                                "[2026-06-04T12:00:03+00:00] ERROR Bad supplier response",
                            ]
                        ),
                    ),
                ), patch("webapp.app.count_ecs_stream_matches", return_value=2):
                    response = client.get(f"/api/jobs/{job_id}/logs")
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertEqual(payload["scraped_count"], 2)
                    self.assertEqual(payload["errors"][-1], "[2026-06-04T12:00:03+00:00] ERROR Bad supplier response")
                    self.assertEqual(
                        payload["cloudwatch_log_group"],
                        "/ecs/yourbarmate-scraper-worker",
                    )

    def test_local_job_logs_endpoint_counts_full_log_not_tail_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "control_panel.db"
            env_path = root / ".env.local"
            config_path = root / "control_panel.json"
            supplier_config_path = root / "suppliers.json"
            log_path = root / "job-1.log"
            env_path.write_text("", encoding="utf-8")
            supplier_config_path.write_text('{"suppliers":[]}', encoding="utf-8")
            os.environ["CONTROL_PANEL_ADMIN_PASSWORD"] = "admin-pass"
            os.environ["CONTROL_PANEL_SESSION_SECRET"] = "session-secret-for-tests"
            config_path.write_text(
                json.dumps(
                    {
                        "db_path": str(db_path),
                        "supplier_config_path": str(supplier_config_path),
                        "env_file": str(env_path),
                        "scheduler_enabled": False,
                        "allowed_artifact_roots": ["output", "logs", "state"],
                        "bootstrap_users": [
                            {
                                "username": "admin",
                                "password_env_var": "CONTROL_PANEL_ADMIN_PASSWORD",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            lines = [f"2026-06-04 12:00:{i:02d},000 INFO Parsed product Item {i} sku={i}" for i in range(250)]
            log_path.write_text("\n".join(lines), encoding="utf-8")

            app = create_app(config_path)
            conn = app.state.db
            job_id = conn.execute(
                """
                INSERT INTO jobs (
                    supplier_slug, job_type, status, requested_by, requested_at,
                    command, env_file_ref, backend, log_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "swissbox",
                    "scrape_only",
                    "running",
                    "admin",
                    "2026-06-04T12:00:00+00:00",
                    json.dumps(["python", "scraper.py", "scrape-supplier", "swissbox"]),
                    str(env_path),
                    "local_subprocess",
                    str(log_path),
                ),
            ).lastrowid
            conn.commit()

            with TestClient(app) as client:
                client.post(
                    "/login",
                    data={"username": "admin", "password": "admin-pass"},
                    follow_redirects=True,
                )
                response = client.get(f"/api/jobs/{job_id}/logs")
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["scraped_count"], 250)
