from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from adapters import ADAPTER_REGISTRY, adapter_path_info
from config import PROJECT_ROOT, get_log_root, get_supplier_config, load_supplier_configs
from orchestrator import load_state, service_log_path, supplier_paths

from .config import WebAppConfig
from .jobs import list_jobs, tail_file


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _normalize_env_value(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Token value cannot be empty.")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("Token value must be a single line.")
    return normalized


def _save_file_dashboard_secret(
    app_config: WebAppConfig,
    env_var: str,
    value: str,
) -> Path:
    """Persist a local-development secret override without exposing it in supplier config."""
    path = app_config.resolved_dashboard_secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_lines: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key, _existing = line.split("=", 1)
            if key.strip() == env_var:
                updated_lines.append(f"{env_var}={value}")
                replaced = True
                continue
        updated_lines.append(line)
    if not replaced:
        updated_lines.append(f"{env_var}={value}")

    path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return path


def _save_aws_secrets_manager_secret(
    app_config: WebAppConfig,
    env_var: str,
    value: str,
) -> Path:
    secret_id = app_config.resolved_aws_secrets_manager_secret_id()
    if not secret_id:
        raise ValueError(
            "AWS Secrets Manager secret ID is not configured for dashboard token storage."
        )
    try:
        import boto3  # type: ignore
    except ImportError as exc:
        raise RuntimeError("boto3 is required for AWS Secrets Manager token storage.") from exc

    region = app_config.ecs_backend.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", region_name=region or None)
    try:
        response = client.get_secret_value(SecretId=secret_id)
        raw_payload = response.get("SecretString", "")
        payload = json.loads(raw_payload) if raw_payload else {}
    except Exception as exc:
        raise RuntimeError(f"Could not read AWS Secrets Manager secret {secret_id}.") from exc
    if not isinstance(payload, dict):
        raise ValueError("AWS Secrets Manager secret must contain a JSON object.")

    payload[env_var] = value
    try:
        client.put_secret_value(
            SecretId=secret_id,
            SecretString=json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        raise RuntimeError(
            "Could not save the token to AWS Secrets Manager. "
            "The EC2 control-plane role needs secretsmanager:PutSecretValue."
        ) from exc
    return Path(f"aws-secrets-manager://{secret_id}")


def save_dashboard_secret(app_config: WebAppConfig, env_var_name: str, secret_value: str) -> Path:
    env_var = env_var_name.strip()
    if not env_var:
        raise ValueError("Token env var cannot be empty.")
    value = _normalize_env_value(secret_value)
    backend = app_config.resolved_shared_secrets_backend().lower()
    if backend == "aws-secrets-manager":
        path = _save_aws_secrets_manager_secret(app_config, env_var, value)
    elif backend:
        raise ValueError(f"Unsupported dashboard secrets backend: {backend}")
    else:
        path = _save_file_dashboard_secret(app_config, env_var, value)
    os.environ[env_var] = value
    return path


def read_release_metadata(app_config: WebAppConfig) -> dict[str, Any]:
    release_path = app_config.resolved_db_path().parent / "release.json"
    payload = read_json_file(release_path)
    if not payload:
        return {
            "path": str(release_path),
            "available": False,
            "revision": "",
            "deployed_at": "",
            "source": "",
            "hostname": "",
        }
    return {
        "path": str(release_path),
        "available": True,
        "revision": str(payload.get("revision", "")),
        "deployed_at": str(payload.get("deployed_at", "")),
        "source": str(payload.get("source", "")),
        "hostname": str(payload.get("hostname", "")),
    }


def ecs_runtime_status(app_config: WebAppConfig) -> dict[str, Any]:
    status: dict[str, Any] = {
        "enabled": app_config.job_backend == "ecs_fargate",
        "credentials_ok": False,
        "sts_identity": "",
        "task_definition_ok": False,
        "task_definition_arn": "",
        "error": "",
    }
    if app_config.job_backend != "ecs_fargate":
        return status
    try:
        import boto3  # type: ignore
    except ImportError:
        status["error"] = "boto3 is not installed."
        return status
    try:
        region = app_config.ecs_backend.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        sts = boto3.client("sts", region_name=region or None)
        identity = sts.get_caller_identity()
        status["credentials_ok"] = True
        status["sts_identity"] = identity.get("Arn", "")

        ecs = boto3.client("ecs", region_name=region or None)
        response = ecs.describe_task_definition(taskDefinition=app_config.ecs_backend.task_definition)
        task_definition = response.get("taskDefinition", {})
        status["task_definition_ok"] = bool(task_definition)
        status["task_definition_arn"] = task_definition.get("taskDefinitionArn", "")
    except Exception as exc:
        status["error"] = str(exc)
    return status


def supplier_health_summary(
    conn: sqlite3.Connection,
    app_config: WebAppConfig,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for supplier in load_supplier_configs(app_config.resolved_supplier_config_path()):
        paths = supplier_paths(
            supplier.supplier_slug,
            supplier.output_path(PROJECT_ROOT),
        )
        state = load_state(paths["state_file"], supplier.supplier_slug)
        run_summary = read_json_file(paths["run_summary"])
        sync_report = read_json_file(paths["sync_report"])
        latest_job = next(iter(list_jobs(conn, supplier_slug=supplier.supplier_slug, limit=1)), None)
        structure = supplier_structure_status(supplier.supplier_slug, supplier.scraper_adapter)
        onboarding = onboarding_status(
            supplier_slug=supplier.supplier_slug,
            adapter_available=supplier.scraper_adapter in ADAPTER_REGISTRY,
            structure=structure,
            secret_present=bool(os.environ.get(supplier.ybm_token_env_var, "").strip()),
            run_summary=run_summary,
            sync_report=sync_report,
        )
        summaries.append(
            {
                "supplier_slug": supplier.supplier_slug,
                "adapter": supplier.scraper_adapter,
                "adapter_available": supplier.scraper_adapter in ADAPTER_REGISTRY,
                "enabled": supplier.enabled,
                "base_url": supplier.base_url,
                "schedule": supplier.schedule,
                "catalog_update_policy": supplier.catalog_update_policy,
                "catalog_update_policy_label": describe_catalog_update_policy(
                    supplier.catalog_update_policy
                ),
                "secret_present": bool(os.environ.get(supplier.ybm_token_env_var, "").strip()),
                "token_env_var": supplier.ybm_token_env_var,
                "last_successful_run_at": state.last_successful_run_at,
                "latest_job": latest_job,
                "last_run_status": (
                    "ok"
                    if run_summary.get("validation", {}).get("performed", False)
                    and run_summary.get("validation", {}).get("errors") == []
                    else ("scraped" if run_summary.get("mode") == "scrape_only" else "error")
                ),
                "last_run_summary": run_summary,
                "last_sync_summary": sync_report,
                "product_count": run_summary.get("product_count", 0),
                "validation_warning_count": len(run_summary.get("validation", {}).get("warnings", [])),
                "validation_error_count": len(run_summary.get("validation", {}).get("errors", [])),
                "next_run_display": describe_schedule(supplier.schedule),
                "onboarding": onboarding,
                "structure": structure,
                "output_paths": {
                    key: str(value)
                    for key, value in paths.items()
                    if key not in {"state_file", "output_dir"}
                },
            }
        )
    return summaries


def system_health(app_config: WebAppConfig) -> dict[str, Any]:
    roots = app_config.resolved_artifact_roots()
    return {
        "scheduler_enabled": app_config.scheduler_enabled,
        "scheduler_mode": app_config.scheduler_mode,
        "db_path": str(app_config.resolved_db_path()),
        "env_file": str(app_config.resolved_env_path()),
        "dashboard_secrets_file": str(app_config.resolved_dashboard_secrets_path()),
        "dashboard_secrets_backend": app_config.resolved_shared_secrets_backend() or "local_file",
        "job_backend": app_config.job_backend,
        "ecs_backend": {
            "region": app_config.ecs_backend.region,
            "cluster": app_config.ecs_backend.cluster,
            "task_definition": app_config.ecs_backend.task_definition,
            "container_name": app_config.ecs_backend.container_name,
            "subnets": app_config.ecs_backend.subnets,
            "security_groups": app_config.ecs_backend.security_groups,
            "artifact_bucket": app_config.ecs_backend.artifact_bucket,
            "artifact_prefix": app_config.ecs_backend.artifact_prefix,
        },
        "artifact_roots": [str(root) for root in roots],
        "artifact_roots_writable": {
            str(root): root.exists() and os.access(root, os.W_OK) for root in roots
        },
        "service_log_tail": tail_file(service_log_path()),
        "log_root": str(get_log_root()),
        "configured_suppliers": [
            supplier.supplier_slug
            for supplier in load_supplier_configs(app_config.resolved_supplier_config_path())
        ],
        "available_adapters": sorted(ADAPTER_REGISTRY.keys()),
        "shared_secrets_backend": app_config.resolved_shared_secrets_backend(),
        "shared_secrets_secret_id": app_config.resolved_aws_secrets_manager_secret_id(),
        "release": read_release_metadata(app_config),
        "ecs_runtime_status": ecs_runtime_status(app_config),
    }


def resolve_allowed_artifact(path: str, app_config: WebAppConfig) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    for root in app_config.resolved_artifact_roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PermissionError(f"Artifact path is not allowed: {resolved}")


def supplier_by_slug(slug: str, app_config: WebAppConfig | None = None) -> dict[str, Any]:
    config_path = app_config.resolved_supplier_config_path() if app_config else None
    supplier = get_supplier_config(slug, config_path)
    structure = supplier_structure_status(supplier.supplier_slug, supplier.scraper_adapter)
    return {
        "supplier_slug": supplier.supplier_slug,
        "adapter": supplier.scraper_adapter,
        "adapter_available": supplier.scraper_adapter in ADAPTER_REGISTRY,
        "enabled": supplier.enabled,
        "base_url": supplier.base_url,
        "token_env_var": supplier.ybm_token_env_var,
        "schedule": supplier.schedule,
        "catalog_update_policy": supplier.catalog_update_policy,
        "catalog_update_policy_label": describe_catalog_update_policy(
            supplier.catalog_update_policy
        ),
        "scrape_settings": supplier.scrape_settings,
        "output_dir": supplier.output_dir,
        "ybm_api_base": supplier.ybm_api_base,
        "structure": structure,
    }


def describe_catalog_update_policy(policy: str) -> str:
    normalized = policy.strip().lower()
    if normalized == "delete_missing":
        return "Delete missing items"
    if normalized == "keep_existing":
        return "Keep old items"
    return policy.replace("_", " ").strip().title() if policy else "Delete missing items"


def describe_schedule(schedule: dict[str, Any]) -> str:
    if not schedule:
        return "Not scheduled"
    frequency = str(schedule.get("frequency", "")).strip().lower()
    if frequency == "weekly":
        weekday = str(schedule.get("weekday", "monday")).strip().capitalize()
        time_text = str(schedule.get("time", "03:30")).strip()
        return f"Weekly on {weekday} at {time_text}"
    if frequency == "disabled":
        return "Disabled"
    return json.dumps(schedule, ensure_ascii=False)


def parse_bool_form(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "on", "yes"}


def normalize_schedule_form(
    *,
    enabled: bool,
    frequency: str,
    weekday: str,
    time_text: str,
) -> dict[str, str]:
    if not enabled:
        return {"frequency": "disabled"}
    return {
        "frequency": frequency.strip().lower() or "weekly",
        "weekday": weekday.strip().lower() or "monday",
        "time": time_text.strip() or "03:30",
    }


def build_supplier_from_form(
    *,
    supplier_slug: str,
    enabled: bool,
    scraper_adapter: str,
    base_url: str,
    ybm_token_env_var: str,
    output_dir: str,
    catalog_update_policy: str,
    ybm_api_base: str,
    schedule_enabled: bool,
    schedule_frequency: str,
    schedule_weekday: str,
    schedule_time: str,
    concurrency: str,
    min_delay_seconds: str,
    max_delay_seconds: str,
):
    scrape_settings: dict[str, Any] = {}
    if concurrency.strip():
        scrape_settings["concurrency"] = int(concurrency.strip())
    if min_delay_seconds.strip():
        scrape_settings["min_delay_seconds"] = float(min_delay_seconds.strip())
    if max_delay_seconds.strip():
        scrape_settings["max_delay_seconds"] = float(max_delay_seconds.strip())
    from models import SupplierConfig

    return SupplierConfig(
        supplier_slug=supplier_slug.strip(),
        enabled=enabled,
        scraper_adapter=scraper_adapter.strip(),
        base_url=base_url.strip(),
        ybm_token_env_var=ybm_token_env_var.strip(),
        output_dir=output_dir.strip(),
        catalog_update_policy=catalog_update_policy.strip() or "delete_missing",
        schedule=normalize_schedule_form(
            enabled=schedule_enabled,
            frequency=schedule_frequency,
            weekday=schedule_weekday,
            time_text=schedule_time,
        ),
        ybm_api_base=ybm_api_base.strip() or "https://connect.yourbarmate.com/api",
        scrape_settings=scrape_settings,
    )


def supplier_structure_status(supplier_slug: str, adapter_name: str) -> dict[str, Any]:
    paths = adapter_path_info(supplier_slug)
    scraper_path = Path(paths["scraper_path"])
    transformer_path = Path(paths["transformer_path"])
    fixtures_path = Path(paths["fixtures_path"])
    tests_path = PROJECT_ROOT / paths["tests_hint"]
    return {
        "adapter_package_dir": paths["adapter_package_dir"],
        "scraper_path": str(scraper_path),
        "transformer_path": str(transformer_path),
        "fixtures_path": str(fixtures_path),
        "tests_path": str(tests_path),
        "scraper_exists": scraper_path.exists(),
        "transformer_exists": transformer_path.exists(),
        "fixtures_present": fixtures_path.exists() and any(fixtures_path.iterdir()),
        "tests_present": tests_path.exists(),
        "adapter_matches_slug": adapter_name == supplier_slug,
    }


def onboarding_status(
    *,
    supplier_slug: str,
    adapter_available: bool,
    structure: dict[str, Any],
    secret_present: bool,
    run_summary: dict[str, Any],
    sync_report: dict[str, Any],
) -> dict[str, Any]:
    validation = run_summary.get("validation", {})
    validation_passed = bool(run_summary) and validation.get("performed", False) and not validation.get("errors")
    dry_run_seen = bool(run_summary)
    sync_errors = sync_report.get("errors", []) if isinstance(sync_report, dict) else []
    first_sync_passed = bool(sync_report) and not sync_report.get("aborted") and not sync_errors
    checks = [
        {"label": "Config created", "ok": True},
        {"label": "Secret available", "ok": secret_present},
        {"label": "Adapter implemented", "ok": adapter_available and structure["scraper_exists"]},
        {"label": "Transformer implemented", "ok": structure["transformer_exists"]},
        {"label": "Fixtures/tests present", "ok": structure["fixtures_present"] and structure["tests_present"]},
        {"label": "Dry run passed", "ok": dry_run_seen},
        {"label": "Validation passed", "ok": validation_passed},
        {"label": "First sync passed", "ok": first_sync_passed},
    ]
    live = first_sync_passed and validation_passed and secret_present
    stage = "Config created"
    if not adapter_available or not structure["scraper_exists"]:
        stage = "Config only"
    elif not secret_present:
        stage = "Needs secret"
    elif not structure["transformer_exists"] or not structure["fixtures_present"] or not structure["tests_present"]:
        stage = "Implementation pending"
    elif validation_passed and not first_sync_passed:
        stage = "Ready for sync"
    elif live:
        stage = "Live"
    return {
        "stage": stage,
        "live": live,
        "checks": checks + [{"label": "Live", "ok": live}],
        "dry_run_seen": dry_run_seen,
        "validation_passed": validation_passed,
        "first_sync_passed": first_sync_passed,
    }
