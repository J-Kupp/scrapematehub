from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from config import PROJECT_ROOT, get_log_root, get_supplier_config, load_supplier_configs
from orchestrator import supplier_paths
from webapp.config import EcsBackendConfig
from webapp.ecs_jobs import describe_ecs_task, EcsJobError, launch_ecs_task


JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_job_command(
    supplier_slug: str,
    job_type: str,
    *,
    env_file: Path | None,
) -> list[str]:
    command = [sys.executable, "scraper.py"]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    if job_type == "scrape_dry_run":
        command.extend(["dry-run-supplier", supplier_slug])
    elif job_type == "scrape_and_sync":
        command.extend(["run-supplier", supplier_slug])
    elif job_type == "sync_from_export":
        command.extend(["sync-from-export", supplier_slug])
    else:
        raise ValueError(f"Unsupported job type: {job_type}")
    return command


def build_worker_job_args(
    supplier_slug: str,
    job_type: str,
    *,
    env_file: Path | None,
) -> list[str]:
    args: list[str] = []
    if env_file is not None:
        args.extend(["--env-file", str(env_file)])
    if job_type == "scrape_dry_run":
        args.extend(["dry-run-supplier", supplier_slug])
    elif job_type == "scrape_and_sync":
        args.extend(["run-supplier", supplier_slug])
    elif job_type == "sync_from_export":
        args.extend(["sync-from-export", supplier_slug])
    else:
        raise ValueError(f"Unsupported job type: {job_type}")
    return args


def queue_job(
    conn: sqlite3.Connection,
    *,
    supplier_slug: str,
    job_type: str,
    requested_by: str,
    env_file_ref: str,
    command: list[str],
) -> int:
    duplicate = conn.execute(
        """
        SELECT id FROM jobs
        WHERE supplier_slug = ? AND job_type = ? AND status IN (?, ?)
        ORDER BY id DESC LIMIT 1
        """,
        (supplier_slug, job_type, JOB_STATUS_QUEUED, JOB_STATUS_RUNNING),
    ).fetchone()
    if duplicate is not None:
        raise ValueError(
            f"A {job_type} job for supplier {supplier_slug} is already queued or running."
        )
    job_id = conn.execute(
        """
        INSERT INTO jobs (
            supplier_slug, job_type, status, requested_by, requested_at, command, env_file_ref, backend
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            supplier_slug,
            job_type,
            JOB_STATUS_QUEUED,
            requested_by,
            utc_now(),
            json.dumps(command),
            env_file_ref,
            "local_subprocess",
        ),
    ).lastrowid
    conn.commit()
    return int(job_id)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def list_jobs(
    conn: sqlite3.Connection,
    *,
    supplier_slug: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if supplier_slug:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE supplier_slug = ? ORDER BY id DESC LIMIT ?",
            (supplier_slug, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def tail_file(path: Path, max_bytes: int = 20000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


class JobRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        env_file: Path | None,
        supplier_config_path: Path | None = None,
        job_backend: str = "local_subprocess",
        ecs_backend: EcsBackendConfig | None = None,
    ) -> None:
        self.conn = conn
        self.env_file = env_file
        self.supplier_config_path = supplier_config_path
        self.job_backend = job_backend
        self.ecs_backend = ecs_backend or EcsBackendConfig()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run_loop, daemon=True)
        self.scheduler: BackgroundScheduler | None = None

    def start(self) -> None:
        self._recover_stale_jobs()
        if not self._worker.is_alive():
            self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None

    def reload_scheduler(self, *, enabled: bool, mode: str) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
        self.start_scheduler(enabled=enabled, mode=mode)

    def start_scheduler(self, *, enabled: bool, mode: str) -> None:
        if not enabled or mode != "internal":
            return
        scheduler = BackgroundScheduler(timezone="Europe/Zurich")
        for supplier in load_supplier_configs(self.supplier_config_path):
            if not supplier.enabled:
                continue
            schedule = supplier.schedule or {}
            if schedule.get("frequency") != "weekly":
                continue
            weekday = schedule.get("weekday", "monday")
            hour, minute = (schedule.get("time", "03:30").split(":", 1) + ["0"])[:2]
            scheduler.add_job(
                self._enqueue_scheduled_job,
                "cron",
                day_of_week=weekday[:3],
                hour=int(hour),
                minute=int(minute),
                args=[supplier.supplier_slug],
                id=f"supplier-{supplier.supplier_slug}",
                replace_existing=True,
            )
        scheduler.start()
        self.scheduler = scheduler

    def _enqueue_scheduled_job(self, supplier_slug: str) -> None:
        try:
            queue_job(
                self.conn,
                supplier_slug=supplier_slug,
                job_type="scrape_and_sync",
                requested_by="scheduler",
                env_file_ref=str(self.env_file) if self.env_file else "",
                command=build_job_command(
                    supplier_slug,
                    "scrape_and_sync",
                    env_file=self.env_file,
                ),
            )
        except ValueError:
            return

    def _recover_stale_jobs(self) -> None:
        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = ?, error_message = COALESCE(NULLIF(error_message, ''), ?)
            WHERE status = ?
            """,
            (
                JOB_STATUS_FAILED,
                utc_now(),
                "Recovered stale running job during service startup.",
                JOB_STATUS_RUNNING,
            ),
        )
        self.conn.commit()

    def _claim_next_job(self) -> dict[str, Any] | None:
        queued_row = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (JOB_STATUS_QUEUED,),
        ).fetchone()
        if queued_row is None:
            return None
        job = dict(queued_row)
        self.conn.execute(
            "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
            (JOB_STATUS_RUNNING, utc_now(), job["id"]),
        )
        self.conn.commit()
        job["status"] = JOB_STATUS_RUNNING
        return job

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            running = self.conn.execute(
                "SELECT id FROM jobs WHERE status = ? LIMIT 1",
                (JOB_STATUS_RUNNING,),
            ).fetchone()
            if running is not None:
                time.sleep(1.0)
                continue
            job = self._claim_next_job()
            if job is None:
                time.sleep(1.0)
                continue
            self._execute_job(job)

    def _execute_job(self, job: dict[str, Any]) -> None:
        if self.job_backend == "ecs_fargate":
            self._execute_ecs_job(job)
            return
        self._execute_local_job(job)

    def _execute_local_job(self, job: dict[str, Any]) -> None:
        supplier_slug = job["supplier_slug"]
        log_dir = get_log_root() / "webapp" / "jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"job-{job['id']}.log"
        self.conn.execute(
            "UPDATE jobs SET log_path = ? WHERE id = ?",
            (str(log_path), job["id"]),
        )
        self.conn.commit()

        command = json.loads(job["command"])
        env = os.environ.copy()
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(command)}\n")
            handle.flush()
            process = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )

        config = get_supplier_config(supplier_slug)
        paths = supplier_paths(supplier_slug, config.output_path(PROJECT_ROOT))
        status = JOB_STATUS_SUCCEEDED if process.returncode == 0 else JOB_STATUS_FAILED
        error_message = ""
        if process.returncode != 0:
            error_message = tail_file(log_path, max_bytes=4000).splitlines()[-1] if tail_file(log_path) else ""
        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = ?, exit_code = ?, error_message = ?,
                run_summary_path = ?, sync_report_path = ?, log_path = ?, backend = ?
            WHERE id = ?
            """,
            (
                status,
                utc_now(),
                process.returncode,
                error_message,
                str(paths["run_summary"]),
                str(paths["sync_report"]),
                str(log_path),
                "local_subprocess",
                job["id"],
            ),
        )
        self.conn.commit()

    def _execute_ecs_job(self, job: dict[str, Any]) -> None:
        supplier_slug = job["supplier_slug"]
        command = build_worker_job_args(
            supplier_slug,
            job["job_type"],
            env_file=self.env_file,
        )
        config = get_supplier_config(supplier_slug)
        paths = supplier_paths(supplier_slug, config.output_path(PROJECT_ROOT))
        try:
            launch = launch_ecs_task(
                self.ecs_backend,
                command=command,
                env_overrides={
                    "SUPPLIER_SLUG": supplier_slug,
                    "JOB_TYPE": job["job_type"],
                    "ENV_FILE_REF": str(self.env_file) if self.env_file else "",
                },
                supplier_slug=supplier_slug,
                job_type=job["job_type"],
            )
            self.conn.execute(
                """
                UPDATE jobs
                SET backend = ?, remote_job_id = ?, remote_status = ?, artifact_prefix = ?,
                    cloudwatch_log_group = ?, cloudwatch_log_stream = ?
                WHERE id = ?
                """,
                (
                    "ecs_fargate",
                    launch.task_arn,
                    launch.last_status,
                    launch.artifact_prefix,
                    launch.log_group,
                    launch.log_stream,
                    job["id"],
                ),
            )
            self.conn.commit()
            while not self._stop_event.is_set():
                result = describe_ecs_task(self.ecs_backend, task_arn=launch.task_arn)
                self.conn.execute(
                    """
                    UPDATE jobs
                    SET remote_status = ?, cloudwatch_log_group = ?, cloudwatch_log_stream = ?
                    WHERE id = ?
                    """,
                    (result.last_status, result.log_group, result.log_stream, job["id"]),
                )
                self.conn.commit()
                if result.last_status == "STOPPED":
                    status = JOB_STATUS_SUCCEEDED if result.exit_code == 0 else JOB_STATUS_FAILED
                    self.conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, finished_at = ?, exit_code = ?, error_message = ?,
                            run_summary_path = ?, sync_report_path = ?, backend = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            utc_now(),
                            result.exit_code,
                            result.stopped_reason or result.stop_code,
                            str(paths["run_summary"]),
                            str(paths["sync_report"]),
                            "ecs_fargate",
                            job["id"],
                        ),
                    )
                    self.conn.commit()
                    return
                time.sleep(5.0)
        except EcsJobError as exc:
            self.conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, exit_code = ?, error_message = ?, backend = ?
                WHERE id = ?
                """,
                (
                    JOB_STATUS_FAILED,
                    utc_now(),
                    1,
                    str(exc),
                    "ecs_fargate",
                    job["id"],
                ),
            )
            self.conn.commit()
