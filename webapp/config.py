from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from config import DEFAULT_ENV_PATH, PROJECT_ROOT, SUPPLIER_CONFIG_PATH, load_env_file


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
class WebAppConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    db_path: str = "state/control_panel/control_panel.db"
    supplier_config_path: str = str(SUPPLIER_CONFIG_PATH)
    session_secret_env_var: str = "CONTROL_PANEL_SESSION_SECRET"
    env_file: str = ".env.local"
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
        )
    load_env_file(config.resolved_env_path())
    config.validate_runtime()
    return config
