from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Any

from .config import AlertEmailConfig


def automation_alert_reason(
    job: dict[str, Any],
    *,
    status: str,
    error_message: str,
    run_summary: dict[str, Any],
    alert_settings: dict[str, Any],
) -> str:
    """Return a human-readable reason only for noteworthy scheduled-run failures."""
    if job.get("requested_by") != "scheduler":
        return ""
    if status == "failed":
        return error_message or "The automated job failed before it could complete."
    sync_errors = (run_summary.get("stages", {}).get("sync", {}) or {}).get("errors", [])
    if sync_errors:
        return "Sync failed: " + str(sync_errors[0])
    scrape = run_summary.get("stages", {}).get("scrape", {}) or {}
    failures = int(scrape.get("failure_count", run_summary.get("failure_count", 0)) or 0)
    processed = int(scrape.get("raw_record_count", 0) or 0)
    minimum = max(1, int(alert_settings.get("minimum_failures", 10) or 10))
    percentage = max(0.0, float(alert_settings.get("failure_rate_percent", 5) or 5))
    failure_rate = (failures / max(processed, failures, 1)) * 100
    if failures >= minimum and failure_rate >= percentage:
        return f"{failures} product errors out of {max(processed, failures)} processed ({failure_rate:.1f}%)."
    return ""


def send_automation_alert(
    config: AlertEmailConfig,
    *,
    recipient: str,
    supplier_slug: str,
    job_id: int,
    reason: str,
) -> None:
    sender = os.environ.get(config.from_email_env_var, "").strip()
    password = os.environ.get(config.smtp_password_env_var, "").strip()
    username = os.environ.get(config.smtp_username_env_var, "").strip()
    if not recipient.strip() or not config.smtp_host.strip() or not sender or not password:
        raise RuntimeError("Automation alert email is not fully configured.")
    message = EmailMessage()
    message["Subject"] = f"ScrapeMate alert: {supplier_slug} scheduled run needs attention"
    message["From"] = sender
    message["To"] = recipient.strip()
    message.set_content(
        f"The scheduled run for supplier '{supplier_slug}' (job #{job_id}) needs attention.\n\n{reason}\n"
    )
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as smtp:
        if config.use_starttls:
            smtp.starttls()
        smtp.login(username or sender, password)
        smtp.send_message(message)
