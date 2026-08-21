from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from config import PROJECT_ROOT, get_log_root, get_supplier_config, load_supplier_configs
from orchestrator import supplier_paths, write_json
from webapp.config import EcsBackendConfig
from webapp.alerts import automation_alert_reason, send_automation_alert
from webapp.config import AlertEmailConfig
from webapp.ecs_jobs import (
    describe_ecs_task,
    EcsJobError,
    launch_ecs_task,
    read_ecs_task_logs,
    stop_ecs_task,
)


JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_STOPPED = "stopped"
logger = logging.getLogger(__name__)
RESULT_RUN_SUMMARY_RE = re.compile(r"RESULT_RUN_SUMMARY\s+(?P<payload>\{.*\})$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_worker_run_summary(logs: str) -> dict[str, Any]:
    """Read the final structured result emitted by an ephemeral Fargate worker."""
    for line in reversed(logs.splitlines()):
        match = RESULT_RUN_SUMMARY_RE.search(line)
        if not match:
            continue
        try:
            payload = json.loads(match.group("payload"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def persist_ecs_result_artifacts(
    config: EcsBackendConfig,
    *,
    task_arn: str,
    log_group: str,
    log_stream: str,
    paths: dict[str, Path],
) -> dict[str, Any]:
    """Copy the worker's result marker into EC2-local artifacts used by the dashboard."""
    for _attempt in range(3):
        _group, _stream, logs = read_ecs_task_logs(
            config,
            task_arn=task_arn,
            log_group=log_group,
            log_stream=log_stream,
            limit=500,
        )
        summary = extract_worker_run_summary(logs)
        if summary:
            write_json(paths["run_summary"], summary)
            sync_summary = summary.get("sync_summary")
            if isinstance(sync_summary, dict):
                write_json(paths["sync_report"], sync_summary)
            return summary
        time.sleep(1.0)
    return {}


def build_job_command(
    supplier_slug: str,
    job_type: str,
    *,
    env_file: Path | None,
) -> list[str]:
    command = [sys.executable, "scraper.py"]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    if job_type == "scrape_only":
        command.extend(["scrape-supplier", supplier_slug])
    elif job_type == "scrape_dry_run":
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
    if job_type == "scrape_only":
        args.extend(["scrape-supplier", supplier_slug])
    elif job_type == "scrape_dry_run":
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


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


class JobRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        env_file: Path | None,
        supplier_config_path: Path | None = None,
        job_backend: str = "local_subprocess",
        ecs_backend: EcsBackendConfig | None = None,
        alert_email: AlertEmailConfig | None = None,
    ) -> None:
        self.conn = conn
        self.env_file = env_file
        self.supplier_config_path = supplier_config_path
        self.job_backend = job_backend
        self.ecs_backend = ecs_backend or EcsBackendConfig()
        self.alert_email = alert_email or AlertEmailConfig()
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._active_local_processes: dict[int, subprocess.Popen[str]] = {}
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

    def stop_job(self, job_id: int, *, reason: str = "Stopped by user.") -> dict[str, Any]:
        job = get_job(self.conn, job_id)
        if job is None:
            raise KeyError(f"Job {job_id} not found.")
        if job.get("status") in {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_STOPPED}:
            return job

        stop_at = utc_now()
        if job.get("status") == JOB_STATUS_QUEUED:
            self.conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, stop_requested_at = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    JOB_STATUS_STOPPED,
                    stop_at,
                    stop_at,
                    reason,
                    job_id,
                ),
            )
            self.conn.commit()
            return get_job(self.conn, job_id) or job

        self.conn.execute(
            """
            UPDATE jobs
            SET stop_requested_at = ?, error_message = COALESCE(NULLIF(error_message, ''), ?)
            WHERE id = ?
            """,
            (stop_at, reason, job_id),
        )
        self.conn.commit()

        if job.get("backend") == "ecs_fargate" and job.get("remote_job_id"):
            stop_ecs_task(self.ecs_backend, task_arn=str(job["remote_job_id"]), reason=reason)
            return get_job(self.conn, job_id) or job

        with self._state_lock:
            process = self._active_local_processes.get(job_id)
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    pass
        return get_job(self.conn, job_id) or job

    def start_scheduler(self, *, enabled: bool, mode: str) -> None:
        if not enabled or mode != "internal":
            return
        scheduler = BackgroundScheduler(timezone="Europe/Zurich")
        self.scheduler = scheduler
        scheduler.start()
        for supplier in load_supplier_configs(self.supplier_config_path):
            if supplier.archived:
                self._record_scheduler_state(
                    supplier.supplier_slug,
                    status="archived",
                    error="Supplier is archived.",
                )
                continue
            if not supplier.enabled:
                self._record_scheduler_state(
                    supplier.supplier_slug,
                    status="blocked",
                    error="Supplier is disabled.",
                )
                continue
            schedule = supplier.schedule or {}
            if schedule.get("frequency") != "weekly":
                self._record_scheduler_state(
                    supplier.supplier_slug,
                    status="disabled",
                    error="Schedule is disabled or unsupported.",
                )
                continue
            try:
                weekday = schedule.get("weekday", "monday")
                hour, minute = (schedule.get("time", "03:30").split(":", 1) + ["0"])[:2]
                job = scheduler.add_job(
                    self._enqueue_scheduled_job,
                    "cron",
                    day_of_week=weekday[:3],
                    hour=int(hour),
                    minute=int(minute),
                    args=[supplier.supplier_slug],
                    id=f"supplier-{supplier.supplier_slug}",
                    replace_existing=True,
                )
                next_run_at = job.next_run_time.isoformat() if job.next_run_time else ""
                self._record_scheduler_state(
                    supplier.supplier_slug,
                    status="scheduled",
                    next_run_at=next_run_at,
                )
                logger.info("Scheduled %s for %s", supplier.supplier_slug, next_run_at)
            except (TypeError, ValueError) as exc:
                self._record_scheduler_state(
                    supplier.supplier_slug,
                    status="invalid",
                    error=f"Invalid schedule: {exc}",
                )
                logger.exception("Unable to schedule supplier %s", supplier.supplier_slug)

    def _enqueue_scheduled_job(self, supplier_slug: str) -> None:
        try:
            job_id = queue_job(
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
            self._record_scheduler_state(
                supplier_slug,
                status="queued",
                next_run_at=self._next_run_at(supplier_slug),
                enqueued=True,
            )
            logger.info("Scheduler queued job %s for %s", job_id, supplier_slug)
        except ValueError as exc:
            self._record_scheduler_state(
                supplier_slug,
                status="skipped",
                next_run_at=self._next_run_at(supplier_slug),
                error=str(exc),
            )
            logger.warning("Scheduler skipped %s: %s", supplier_slug, exc)
        except Exception as exc:  # pragma: no cover - defensive production path
            self._record_scheduler_state(
                supplier_slug,
                status="failed",
                next_run_at=self._next_run_at(supplier_slug),
                error=str(exc),
            )
            logger.exception("Scheduler failed to queue %s", supplier_slug)

    def _next_run_at(self, supplier_slug: str) -> str:
        job = self.scheduler.get_job(f"supplier-{supplier_slug}") if self.scheduler else None
        return job.next_run_time.isoformat() if job and job.next_run_time else ""

    def _record_scheduler_state(
        self,
        supplier_slug: str,
        *,
        status: str,
        next_run_at: str = "",
        error: str = "",
        enqueued: bool = False,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO scheduler_runs (
                supplier_slug, next_run_at, last_enqueued_at, last_status, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(supplier_slug) DO UPDATE SET
                next_run_at = excluded.next_run_at,
                last_enqueued_at = CASE
                    WHEN excluded.last_enqueued_at != '' THEN excluded.last_enqueued_at
                    ELSE scheduler_runs.last_enqueued_at
                END,
                last_status = excluded.last_status,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (supplier_slug, next_run_at, now if enqueued else "", status, error, now),
        )
        self.conn.commit()

    def _recover_stale_jobs(self) -> None:
        running_rows = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = ?
            ORDER BY id ASC
            """,
            (JOB_STATUS_RUNNING,),
        ).fetchall()
        for row in running_rows:
            job = dict(row)
            if job.get("stop_requested_at"):
                if job.get("backend") == "ecs_fargate" and job.get("remote_job_id"):
                    try:
                        result = describe_ecs_task(self.ecs_backend, task_arn=job["remote_job_id"])
                    except Exception:
                        continue
                    self.conn.execute(
                        """
                        UPDATE jobs
                        SET remote_status = ?, cloudwatch_log_group = ?, cloudwatch_log_stream = ?
                        WHERE id = ?
                        """,
                        (result.last_status, result.log_group, result.log_stream, job["id"]),
                    )
                    if result.last_status == "STOPPED":
                        self.conn.execute(
                            """
                            UPDATE jobs
                            SET status = ?, finished_at = ?, exit_code = ?, error_message = ?,
                                backend = ?
                            WHERE id = ?
                            """,
                            (
                                JOB_STATUS_STOPPED,
                                utc_now(),
                                result.exit_code,
                                job.get("error_message") or result.stopped_reason or result.stop_code or "Stopped by user.",
                                "ecs_fargate",
                                job["id"],
                            ),
                        )
                    self.conn.commit()
                    continue
                self.conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, finished_at = ?, exit_code = ?, error_message = ?
                    WHERE id = ?
                    """,
                    (
                        JOB_STATUS_STOPPED,
                        utc_now(),
                        job.get("exit_code") if job.get("exit_code") is not None else 0,
                        job.get("error_message") or "Stopped by user.",
                        job["id"],
                    ),
                )
                self.conn.commit()
                self._notify_scheduled_job(job, JOB_STATUS_FAILED, str(exc), {})
                continue
            if job.get("backend") == "ecs_fargate" and job.get("remote_job_id"):
                try:
                    result = describe_ecs_task(self.ecs_backend, task_arn=job["remote_job_id"])
                except Exception:
                    continue
                self.conn.execute(
                    """
                    UPDATE jobs
                    SET remote_status = ?, cloudwatch_log_group = ?, cloudwatch_log_stream = ?
                    WHERE id = ?
                    """,
                    (result.last_status, result.log_group, result.log_stream, job["id"]),
                )
                if result.last_status == "STOPPED":
                    config = self._supplier_config_for_job(job["supplier_slug"])
                    paths = supplier_paths(job["supplier_slug"], config.output_path(PROJECT_ROOT))
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
                continue
            self.conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, error_message = COALESCE(NULLIF(error_message, ''), ?)
                WHERE id = ?
                """,
                (
                    JOB_STATUS_FAILED,
                    utc_now(),
                    "Recovered stale running job during service startup.",
                    job["id"],
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
            self._recover_stale_jobs()
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
            try:
                self._execute_job(job)
            except Exception as exc:  # pragma: no cover - safety net for background thread
                self.conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, finished_at = ?, exit_code = ?, error_message = ?
                    WHERE id = ?
                    """,
                    (
                        JOB_STATUS_FAILED,
                        utc_now(),
                        1,
                        str(exc),
                        job["id"],
                    ),
                )
                self.conn.commit()

    def _execute_job(self, job: dict[str, Any]) -> None:
        runtime_backend = self._job_runtime_backend(job["job_type"])
        if runtime_backend == "ecs_fargate":
            self._execute_ecs_job(job)
            return
        self._execute_local_job(job)

    def _job_stop_requested(self, job_id: int) -> bool:
        row = self.conn.execute(
            "SELECT stop_requested_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return bool(row and row["stop_requested_at"])

    def _job_runtime_backend(self, job_type: str) -> str:
        if self.job_backend == "ecs_fargate" and job_type in {"scrape_only", "sync_from_export"}:
            return "local_subprocess"
        return self.job_backend

    def _supplier_config_for_job(self, supplier_slug: str):
        """Read the durable dashboard configuration for every execution backend."""
        return get_supplier_config(supplier_slug, self.supplier_config_path)

    def _supplier_config_payload(self, supplier_slug: str) -> str:
        supplier = self._supplier_config_for_job(supplier_slug)
        return json.dumps({"suppliers": [asdict(supplier)]}, ensure_ascii=False)

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
        env["SUPPLIER_CONFIG_JSON"] = self._supplier_config_payload(supplier_slug)
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(command)}\n")
            handle.flush()
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.conn.execute(
                "UPDATE jobs SET local_process_pid = ? WHERE id = ?",
                (process.pid, job["id"]),
            )
            self.conn.commit()
            with self._state_lock:
                self._active_local_processes[job["id"]] = process
            try:
                while process.poll() is None:
                    if self._job_stop_requested(job["id"]) or self._stop_event.is_set():
                        try:
                            process.terminate()
                        except Exception:
                            pass
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            try:
                                process.kill()
                            except Exception:
                                pass
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                pass
                        break
                    time.sleep(0.5)
                return_code = process.wait()
            finally:
                with self._state_lock:
                    self._active_local_processes.pop(job["id"], None)
                self.conn.execute(
                    "UPDATE jobs SET local_process_pid = NULL WHERE id = ?",
                    (job["id"],),
                )
                self.conn.commit()

        config = self._supplier_config_for_job(supplier_slug)
        paths = supplier_paths(supplier_slug, config.output_path(PROJECT_ROOT))
        stopped = self._job_stop_requested(job["id"])
        status = JOB_STATUS_STOPPED if stopped else (JOB_STATUS_SUCCEEDED if return_code == 0 else JOB_STATUS_FAILED)
        error_message = ""
        if stopped:
            error_message = job.get("error_message") or "Stopped by user."
        elif return_code != 0:
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
                return_code,
                error_message,
                str(paths["run_summary"]),
                str(paths["sync_report"]),
                str(log_path),
                "local_subprocess",
                job["id"],
            ),
        )
        self.conn.commit()
        self._notify_scheduled_job(job, status, error_message, read_json_file(paths["run_summary"]))

    def _execute_ecs_job(self, job: dict[str, Any]) -> None:
        supplier_slug = job["supplier_slug"]
        command = build_worker_job_args(
            supplier_slug,
            job["job_type"],
            env_file=self.env_file,
        )
        config = self._supplier_config_for_job(supplier_slug)
        paths = supplier_paths(supplier_slug, config.output_path(PROJECT_ROOT))
        supplier_config_payload = self._supplier_config_payload(supplier_slug)
        try:
            launch = launch_ecs_task(
                self.ecs_backend,
                command=command,
                env_overrides={
                    "SUPPLIER_SLUG": supplier_slug,
                    "JOB_TYPE": job["job_type"],
                    "ENV_FILE_REF": str(self.env_file) if self.env_file else "",
                    "SUPPLIER_CONFIG_JSON": supplier_config_payload,
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
                if self._job_stop_requested(job["id"]):
                    try:
                        stop_ecs_task(
                            self.ecs_backend,
                            task_arn=launch.task_arn,
                            reason="Stopped by user.",
                        )
                    except Exception:
                        pass
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
                    stopped = self._job_stop_requested(job["id"])
                    status = JOB_STATUS_STOPPED if stopped else (JOB_STATUS_SUCCEEDED if result.exit_code == 0 else JOB_STATUS_FAILED)
                    if not stopped:
                        persist_ecs_result_artifacts(
                            self.ecs_backend,
                            task_arn=launch.task_arn,
                            log_group=result.log_group,
                            log_stream=result.log_stream,
                            paths=paths,
                        )
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
                            job.get("error_message") or result.stopped_reason or result.stop_code or ("Stopped by user." if stopped else ""),
                            str(paths["run_summary"]),
                            str(paths["sync_report"]),
                            "ecs_fargate",
                            job["id"],
                        ),
                    )
                    self.conn.commit()
                    self._notify_scheduled_job(
                        job,
                        status,
                        job.get("error_message") or result.stopped_reason or result.stop_code or "",
                        read_json_file(paths["run_summary"]),
                    )
                    return
                time.sleep(5.0)
        except Exception as exc:
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
            self._notify_scheduled_job(job, JOB_STATUS_FAILED, str(exc), {})

    def _notify_scheduled_job(
        self,
        job: dict[str, Any],
        status: str,
        error_message: str,
        run_summary: dict[str, Any],
    ) -> None:
        try:
            supplier = get_supplier_config(job["supplier_slug"], self.supplier_config_path)
            settings = getattr(supplier, "alert_settings", {}) or {}
            reason = automation_alert_reason(
                job,
                status=status,
                error_message=error_message,
                run_summary=run_summary,
                alert_settings=settings,
            )
            recipient = str(settings.get("email_to", "")).strip()
            if not reason or not recipient:
                return
            send_automation_alert(
                self.alert_email,
                recipient=recipient,
                supplier_slug=supplier.supplier_slug,
                job_id=int(job["id"]),
                reason=reason,
            )
            logger.info("Sent scheduled-run alert for %s job %s", supplier.supplier_slug, job["id"])
        except Exception as exc:  # pragma: no cover - delivery configuration is environment-specific
            logger.error("Could not send scheduled-run alert for job %s: %s", job["id"], exc)
