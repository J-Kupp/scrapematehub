from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_slug TEXT NOT NULL,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            stop_requested_at TEXT,
            command TEXT NOT NULL,
            env_file_ref TEXT,
            run_summary_path TEXT,
            sync_report_path TEXT,
            log_path TEXT,
            error_message TEXT,
            exit_code INTEGER,
            local_process_pid INTEGER,
            backend TEXT NOT NULL DEFAULT 'local_subprocess',
            remote_job_id TEXT,
            remote_status TEXT,
            cloudwatch_log_group TEXT,
            cloudwatch_log_stream TEXT,
            artifact_prefix TEXT
        );

        CREATE TABLE IF NOT EXISTS scheduler_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_slug TEXT NOT NULL UNIQUE,
            next_run_at TEXT,
            last_enqueued_at TEXT,
            last_status TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status_requested_at
        ON jobs(status, requested_at);

        CREATE INDEX IF NOT EXISTS idx_jobs_supplier_status
        ON jobs(supplier_slug, status);
        """
    )
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    migration_columns = {
        "backend": "ALTER TABLE jobs ADD COLUMN backend TEXT NOT NULL DEFAULT 'local_subprocess'",
        "remote_job_id": "ALTER TABLE jobs ADD COLUMN remote_job_id TEXT",
        "remote_status": "ALTER TABLE jobs ADD COLUMN remote_status TEXT",
        "cloudwatch_log_group": "ALTER TABLE jobs ADD COLUMN cloudwatch_log_group TEXT",
        "cloudwatch_log_stream": "ALTER TABLE jobs ADD COLUMN cloudwatch_log_stream TEXT",
        "artifact_prefix": "ALTER TABLE jobs ADD COLUMN artifact_prefix TEXT",
        "stop_requested_at": "ALTER TABLE jobs ADD COLUMN stop_requested_at TEXT",
        "local_process_pid": "ALTER TABLE jobs ADD COLUMN local_process_pid INTEGER",
    }
    for name, statement in migration_columns.items():
        if name not in columns:
            conn.execute(statement)
    scheduler_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(scheduler_runs)").fetchall()
    }
    scheduler_migrations = {
        "last_enqueued_at": "ALTER TABLE scheduler_runs ADD COLUMN last_enqueued_at TEXT",
        "last_status": "ALTER TABLE scheduler_runs ADD COLUMN last_status TEXT",
        "last_error": "ALTER TABLE scheduler_runs ADD COLUMN last_error TEXT",
    }
    for name, statement in scheduler_migrations.items():
        if name not in scheduler_columns:
            conn.execute(statement)
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)
