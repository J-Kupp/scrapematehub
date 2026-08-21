from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from config import DEFAULT_ENV_PATH, PROJECT_ROOT, SUPPLIER_CONFIG_PATH, load_env_file
from shared_secrets import load_shared_secrets


WEBAPP_CONFIG_PATH = PROJECT_ROOT / "control_panel.json"
WEBAPP_CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "control_panel.example.json"


@dataclass
class BootstrapUser:
    username: str
    password_env_var: str


@dataclass
class EcsBackendConfig:
    region: str = ""
    cluster: str = ""
    task_definition: str = ""
    container_name: str = ""
    subnets: list[str] = field(default_factory=list)
    security_groups: list[str] = field(default_factory=list)
    assign_public_ip: bool = True
    launch_type: str = "FARGATE"
    platform_version: str = "LATEST"
    execution_role_mode: str = "task"
    artifact_bucket: str = ""
    artifact_prefix: str = "suppliers"


@dataclass
class AlertEmailConfig:
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username_env_var: str = "ALERT_SMTP_USERNAME"
    smtp_password_env_var: str = "ALERT_SMTP_PASSWORD"
    from_email_env_var: str = "ALERT_EMAIL_FROM"
    use_starttls: bool = True


@dataclass
class WebAppConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    db_path: str = "state/control_panel/control_panel.db"
    supplier_config_path: str = str(SUPPLIER_CONFIG_PATH)
    session_secret_env_var: str = "CONTROL_PANEL_SESSION_SECRET"
    env_file: str = ".env.local"
    dashboard_secrets_file: str = ""
    shared_secrets_backend: str = ""
    aws_secrets_manager_secret_id: str = ""
    shared_secrets_region: str = ""
    scheduler_enabled: bool = True
    scheduler_mode: str = "internal"
    session_same_site: str = "lax"
    session_https_only: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    enforce_non_placeholder_secrets: bool = False
    job_backend: str = "local_subprocess"
    allowed_artifact_roots: list[str] = field(
        default_factory=lambda: ["output", "logs", "state"]
    )
    bootstrap_users: list[BootstrapUser] = field(default_factory=list)
    ecs_backend: EcsBackendConfig = field(default_factory=EcsBackendConfig)
    alert_email: AlertEmailConfig = field(default_factory=AlertEmailConfig)

    def resolved_db_path(self) -> Path:
        path = Path(self.db_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    def resolved_supplier_config_path(self) -> Path:
        path = Path(self.supplier_config_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    def resolved_env_path(self) -> Path:
        path = Path(self.env_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    def resolved_dashboard_secrets_path(self) -> Path:
        if self.dashboard_secrets_file.strip():
            path = Path(self.dashboard_secrets_file)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            return path
        return self.resolved_db_path().parent / "dashboard-secrets.env"

    def resolved_shared_secrets_backend(self) -> str:
        return (
            self.shared_secrets_backend.strip()
            or os.environ.get("SHARED_SECRETS_BACKEND", "").strip()
        )

    def resolved_aws_secrets_manager_secret_id(self) -> str:
        return (
            self.aws_secrets_manager_secret_id.strip()
            or os.environ.get("AWS_SECRETS_MANAGER_SECRET_ID", "").strip()
        )

    def resolved_shared_secrets_region(self) -> str:
        return (
            self.shared_secrets_region.strip()
            or os.environ.get("AWS_REGION", "").strip()
            or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        )

    def resolved_artifact_roots(self) -> list[Path]:
        roots: list[Path] = []
        for root in self.allowed_artifact_roots:
            path = Path(root)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            roots.append(path.resolve())
        return roots

    def session_secret(self) -> str:
        secret = os.environ.get(self.session_secret_env_var, "").strip()
        if secret:
            return secret
        return "replace-me-before-exposing-control-panel"

    def validate_runtime(self) -> None:
        if self.enforce_non_placeholder_secrets:
            secret = self.session_secret()
            if not secret or "replace" in secret.lower():
                raise ValueError(
                    f"{self.session_secret_env_var} must be set to a non-placeholder value."
                )
            for user in self.bootstrap_users:
                password = os.environ.get(user.password_env_var, "").strip()
                if not password or "replace" in password.lower():
                    raise ValueError(
                        f"{user.password_env_var} must be set to a non-placeholder value."
                    )
        if self.job_backend == "ecs_fargate":
            missing = []
            if not self.ecs_backend.region:
                missing.append("ecs_backend.region")
            if not self.ecs_backend.cluster:
                missing.append("ecs_backend.cluster")
            if not self.ecs_backend.task_definition:
                missing.append("ecs_backend.task_definition")
            if not self.ecs_backend.container_name:
                missing.append("ecs_backend.container_name")
            if not self.ecs_backend.subnets:
                missing.append("ecs_backend.subnets")
            if missing:
                raise ValueError(
                    "ECS Fargate backend requires: " + ", ".join(missing)
                )


def default_webapp_config() -> WebAppConfig:
    return WebAppConfig(
        env_file=str(DEFAULT_ENV_PATH),
        bootstrap_users=[
            BootstrapUser(
                username="admin",
                password_env_var="CONTROL_PANEL_ADMIN_PASSWORD",
            )
        ],
    )


def load_webapp_config(config_path: Path | None = None) -> WebAppConfig:
    path = config_path or WEBAPP_CONFIG_PATH
    config = default_webapp_config()
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        bootstrap_users = [
            BootstrapUser(**user_payload)
            for user_payload in payload.get("bootstrap_users", [])
        ]
        ecs_backend_payload = payload.get("ecs_backend", {})
        alert_email_payload = payload.get("alert_email", {})
        config = WebAppConfig(
            host=payload.get("host", config.host),
            port=int(payload.get("port", config.port)),
            db_path=payload.get("db_path", config.db_path),
            supplier_config_path=payload.get(
                "supplier_config_path", config.supplier_config_path
            ),
            session_secret_env_var=payload.get(
                "session_secret_env_var", config.session_secret_env_var
            ),
            env_file=payload.get("env_file", config.env_file),
            dashboard_secrets_file=payload.get(
                "dashboard_secrets_file", config.dashboard_secrets_file
            ),
            shared_secrets_backend=payload.get(
                "shared_secrets_backend", config.shared_secrets_backend
            ),
            aws_secrets_manager_secret_id=payload.get(
                "aws_secrets_manager_secret_id",
                config.aws_secrets_manager_secret_id,
            ),
            shared_secrets_region=payload.get(
                "shared_secrets_region", config.shared_secrets_region
            ),
            scheduler_enabled=bool(
                payload.get("scheduler_enabled", config.scheduler_enabled)
            ),
            scheduler_mode=payload.get("scheduler_mode", config.scheduler_mode),
            session_same_site=payload.get("session_same_site", config.session_same_site),
            session_https_only=bool(
                payload.get("session_https_only", config.session_https_only)
            ),
            forwarded_allow_ips=payload.get(
                "forwarded_allow_ips", config.forwarded_allow_ips
            ),
            enforce_non_placeholder_secrets=bool(
                payload.get(
                    "enforce_non_placeholder_secrets",
                    config.enforce_non_placeholder_secrets,
                )
            ),
            job_backend=payload.get("job_backend", config.job_backend),
            allowed_artifact_roots=payload.get(
                "allowed_artifact_roots", config.allowed_artifact_roots
            ),
            bootstrap_users=bootstrap_users or config.bootstrap_users,
            ecs_backend=EcsBackendConfig(
                region=ecs_backend_payload.get("region", ""),
                cluster=ecs_backend_payload.get("cluster", ""),
                task_definition=ecs_backend_payload.get("task_definition", ""),
                container_name=ecs_backend_payload.get("container_name", ""),
                subnets=ecs_backend_payload.get("subnets", []),
                security_groups=ecs_backend_payload.get("security_groups", []),
                assign_public_ip=bool(ecs_backend_payload.get("assign_public_ip", True)),
                launch_type=ecs_backend_payload.get("launch_type", "FARGATE"),
                platform_version=ecs_backend_payload.get("platform_version", "LATEST"),
                execution_role_mode=ecs_backend_payload.get("execution_role_mode", "task"),
                artifact_bucket=ecs_backend_payload.get("artifact_bucket", ""),
                artifact_prefix=ecs_backend_payload.get("artifact_prefix", "suppliers"),
            ),
            alert_email=AlertEmailConfig(
                smtp_host=alert_email_payload.get("smtp_host", ""),
                smtp_port=int(alert_email_payload.get("smtp_port", 587)),
                smtp_username_env_var=alert_email_payload.get(
                    "smtp_username_env_var", "ALERT_SMTP_USERNAME"
                ),
                smtp_password_env_var=alert_email_payload.get(
                    "smtp_password_env_var", "ALERT_SMTP_PASSWORD"
                ),
                from_email_env_var=alert_email_payload.get("from_email_env_var", "ALERT_EMAIL_FROM"),
                use_starttls=bool(alert_email_payload.get("use_starttls", True)),
            ),
        )
    # Production config is the source of truth; the env file may still be used locally.
    if config.shared_secrets_backend.strip():
        os.environ.setdefault("SHARED_SECRETS_BACKEND", config.shared_secrets_backend.strip())
    if config.aws_secrets_manager_secret_id.strip():
        os.environ.setdefault(
            "AWS_SECRETS_MANAGER_SECRET_ID",
            config.aws_secrets_manager_secret_id.strip(),
        )
    shared_secrets_region = config.shared_secrets_region.strip() or config.ecs_backend.region
    if shared_secrets_region:
        os.environ.setdefault("AWS_REGION", shared_secrets_region)
    load_env_file(config.resolved_env_path())
    shared_backend = config.resolved_shared_secrets_backend()
    if shared_backend:
        # The configured shared store is authoritative in production. This prevents
        # retired local overrides from silently replacing a token after restart.
        load_shared_secrets(overwrite=True)
    secrets_path = config.resolved_dashboard_secrets_path()
    if not shared_backend and secrets_path.exists():
        for raw_line in secrets_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and value:
                os.environ[key] = value
    config.validate_runtime()
    return config
