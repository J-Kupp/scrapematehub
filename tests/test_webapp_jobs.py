from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from webapp.db import connect, init_db
from webapp.jobs import (
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JobRunner,
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
                "--env-file",
                "/etc/yourbarmate-suppliers.env",
                "dry-run-supplier",
                "swissbox",
            ],
        )
