from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webapp.config import EcsBackendConfig
from webapp.db import connect, init_db
from webapp.jobs import (
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_STOPPED,
    JOB_STATUS_SUCCEEDED,
    JobRunner,
    build_job_command,
    build_worker_job_args,
    get_job,
    queue_job,
)


class WebAppJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tempdir.name) / "control_panel.db")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_queue_job_rejects_duplicate_supplier_and_type_while_queued(self) -> None:
        queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_dry_run",
            requested_by="admin",
            env_file_ref=".env.local",
            command=["python", "scraper.py", "dry-run-supplier", "swissbox"],
        )
        with self.assertRaisesRegex(ValueError, "already queued or running"):
            queue_job(
                self.conn,
                supplier_slug="swissbox",
                job_type="scrape_dry_run",
                requested_by="admin",
                env_file_ref=".env.local",
                command=["python", "scraper.py", "dry-run-supplier", "swissbox"],
            )

    def test_queue_job_allows_different_job_types(self) -> None:
        first_job_id = queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_dry_run",
            requested_by="admin",
            env_file_ref=".env.local",
            command=["python", "scraper.py", "dry-run-supplier", "swissbox"],
        )
        second_job_id = queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="sync_from_export",
            requested_by="admin",
            env_file_ref=".env.local",
            command=["python", "scraper.py", "sync-from-export", "swissbox"],
        )
        self.assertGreater(second_job_id, first_job_id)

    def test_get_job_returns_status_fields(self) -> None:
        job_id = queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_and_sync",
            requested_by="admin",
            env_file_ref=".env.local",
            command=["python", "scraper.py", "run-supplier", "swissbox"],
        )
        job = get_job(self.conn, job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], JOB_STATUS_QUEUED)
        self.assertIn(job["status"], {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED})

    def test_stop_job_marks_queued_job_stopped(self) -> None:
        job_id = queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_dry_run",
            requested_by="admin",
            env_file_ref=".env.local",
            command=["python", "scraper.py", "dry-run-supplier", "swissbox"],
        )
        runner = JobRunner(self.conn, env_file=None)
        updated_job = runner.stop_job(job_id)

        self.assertEqual(updated_job["status"], JOB_STATUS_STOPPED)
        self.assertIsNotNone(updated_job["stop_requested_at"])
        self.assertIsNotNone(updated_job["finished_at"])

    def test_job_runner_recovers_stale_running_jobs_on_start(self) -> None:
        job_id = queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_and_sync",
            requested_by="admin",
            env_file_ref=".env.local",
            command=["python", "scraper.py", "run-supplier", "swissbox"],
        )
        self.conn.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            (JOB_STATUS_RUNNING, job_id),
        )
        self.conn.commit()

        runner = JobRunner(self.conn, env_file=None)
        runner._recover_stale_jobs()

        job = get_job(self.conn, job_id)
        self.assertEqual(job["status"], JOB_STATUS_FAILED)
        self.assertIn("Recovered stale running job", job["error_message"])

    def test_stop_job_requests_ecs_task_stop_for_running_job(self) -> None:
        job_id = queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_dry_run",
            requested_by="admin",
            env_file_ref="/etc/yourbarmate-suppliers.env",
            command=["python", "scraper.py", "dry-run-supplier", "swissbox"],
        )
        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?, started_at = ?, backend = ?, remote_job_id = ?, remote_status = ?
            WHERE id = ?
            """,
            (
                JOB_STATUS_RUNNING,
                "2026-06-01T00:00:00+00:00",
                "ecs_fargate",
                "arn:aws:ecs:eu-central-1:123456789012:task/cluster/task-123",
                "RUNNING",
                job_id,
            ),
        )
        self.conn.commit()
        runner = JobRunner(
            self.conn,
            env_file=Path("/etc/yourbarmate-suppliers.env"),
            job_backend="ecs_fargate",
            ecs_backend=EcsBackendConfig(
                region="eu-central-1",
                cluster="cluster",
                task_definition="family",
                container_name="scraper-worker",
                subnets=["subnet-123"],
            ),
        )

        with patch("webapp.jobs.stop_ecs_task") as stop_ecs_task_mock:
            updated_job = runner.stop_job(job_id)

        self.assertEqual(updated_job["status"], JOB_STATUS_RUNNING)
        self.assertIsNotNone(updated_job["stop_requested_at"])
        stop_ecs_task_mock.assert_called_once()

    def test_queue_job_sets_default_backend_metadata(self) -> None:
        job_id = queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_dry_run",
            requested_by="admin",
            env_file_ref=".env.local",
            command=["python", "scraper.py", "dry-run-supplier", "swissbox"],
        )
        job = get_job(self.conn, job_id)
        self.assertEqual(job["backend"], "local_subprocess")

    def test_build_worker_job_args_matches_cli_shape(self) -> None:
        args = build_worker_job_args(
            "swissbox",
            "scrape_dry_run",
            env_file=Path("/etc/yourbarmate-suppliers.env"),
        )
        self.assertEqual(
            args,
            [
                "dry-run-supplier",
                "swissbox",
            ],
        )

    def test_build_job_command_supports_scrape_only(self) -> None:
        command = build_job_command(
            "swissbox",
            "scrape_only",
            env_file=Path("/etc/yourbarmate-suppliers.env"),
        )
        self.assertEqual(
            command,
            [
                unittest.mock.ANY,
                "scraper.py",
                "--env-file",
                "/etc/yourbarmate-suppliers.env",
                "scrape-supplier",
                "swissbox",
            ],
        )

    def test_job_runner_routes_scrape_only_and_sync_from_export_to_local_backend(self) -> None:
        runner = JobRunner(
            self.conn,
            env_file=Path("/etc/yourbarmate-suppliers.env"),
            job_backend="ecs_fargate",
            ecs_backend=EcsBackendConfig(
                region="eu-central-1",
                cluster="cluster",
                task_definition="family",
                container_name="scraper-worker",
                subnets=["subnet-123"],
            ),
        )

        self.assertEqual(runner._job_runtime_backend("scrape_only"), "local_subprocess")
        self.assertEqual(runner._job_runtime_backend("sync_from_export"), "local_subprocess")
        self.assertEqual(runner._job_runtime_backend("scrape_and_sync"), "ecs_fargate")

    def test_ecs_job_runtime_error_marks_job_failed(self) -> None:
        job_id = queue_job(
            self.conn,
            supplier_slug="swissbox",
            job_type="scrape_dry_run",
            requested_by="admin",
            env_file_ref="/etc/yourbarmate-suppliers.env",
            command=["python", "scraper.py", "dry-run-supplier", "swissbox"],
        )
        self.conn.execute(
            "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
            (JOB_STATUS_RUNNING, "2026-06-01T00:00:00+00:00", job_id),
        )
        self.conn.commit()
        runner = JobRunner(
            self.conn,
            env_file=Path("/etc/yourbarmate-suppliers.env"),
            job_backend="ecs_fargate",
            ecs_backend=EcsBackendConfig(
                region="eu-central-1",
                cluster="cluster",
                task_definition="family",
                container_name="scraper-worker",
                subnets=["subnet-123"],
            ),
        )
        job = get_job(self.conn, job_id)
        self.assertIsNotNone(job)

        with patch("webapp.jobs.launch_ecs_task", side_effect=RuntimeError("boom")), patch(
            "webapp.jobs.get_supplier_config"
        ) as get_supplier_config_mock, patch("webapp.jobs.supplier_paths") as supplier_paths_mock:
            get_supplier_config_mock.return_value = type(
                "Supplier",
                (),
                {"output_path": lambda self, root: root / "output" / "swissbox"},
            )()
            supplier_paths_mock.return_value = {
                "run_summary": Path("/tmp/run-summary.json"),
                "sync_report": Path("/tmp/sync-report.json"),
            }
            runner._execute_job(job)

        updated_job = get_job(self.conn, job_id)
        self.assertEqual(updated_job["status"], JOB_STATUS_FAILED)
        self.assertEqual(updated_job["backend"], "ecs_fargate")
        self.assertIn("boom", updated_job["error_message"])

    def test_sync_from_export_uses_local_execution_when_ecs_backend_is_enabled(self) -> None:
        runner = JobRunner(
            self.conn,
            env_file=Path("/etc/yourbarmate-suppliers.env"),
            job_backend="ecs_fargate",
            ecs_backend=EcsBackendConfig(
                region="eu-central-1",
                cluster="cluster",
                task_definition="family",
                container_name="scraper-worker",
                subnets=["subnet-123"],
            ),
        )
        job = {
            "id": 1,
            "supplier_slug": "swissbox",
            "job_type": "sync_from_export",
            "status": JOB_STATUS_RUNNING,
        }

        with patch.object(runner, "_execute_local_job") as local_job_mock, patch.object(
            runner, "_execute_ecs_job"
        ) as ecs_job_mock:
            runner._execute_job(job)

        local_job_mock.assert_called_once_with(job)
        ecs_job_mock.assert_not_called()
