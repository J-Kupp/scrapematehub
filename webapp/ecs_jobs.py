from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from webapp.config import EcsBackendConfig


class EcsJobError(RuntimeError):
    pass


@dataclass
class EcsTaskLaunchResult:
    task_arn: str
    last_status: str
    cluster_arn: str = ""
    log_group: str = ""
    log_stream: str = ""
    artifact_prefix: str = ""


@dataclass
class EcsTaskFinalResult:
    stop_code: str = ""
    stopped_reason: str = ""
    exit_code: int = 0
    last_status: str = ""
    log_group: str = ""
    log_stream: str = ""


def _client(region: str):
    try:
        import boto3  # type: ignore
    except ImportError as exc:
        raise EcsJobError("boto3 is required for the ECS Fargate backend.") from exc
    return boto3.client("ecs", region_name=region or None)


def _task_network_config(config: EcsBackendConfig) -> dict[str, Any]:
    return {
        "awsvpcConfiguration": {
            "subnets": config.subnets,
            "securityGroups": config.security_groups,
            "assignPublicIp": "ENABLED" if config.assign_public_ip else "DISABLED",
        }
    }


def launch_ecs_task(
    config: EcsBackendConfig,
    *,
    command: list[str],
    env_overrides: dict[str, str],
    supplier_slug: str,
    job_type: str,
) -> EcsTaskLaunchResult:
    if not config.cluster or not config.task_definition or not config.container_name:
        raise EcsJobError("ECS backend is missing cluster, task_definition, or container_name.")
    if not config.subnets:
        raise EcsJobError("ECS backend requires at least one subnet.")

    client = _client(config.region)
    overrides = {
        "containerOverrides": [
            {
                "name": config.container_name,
                "command": command,
                "environment": [
                    {"name": key, "value": value}
                    for key, value in sorted(env_overrides.items())
                ],
            }
        ]
    }
    response = client.run_task(
        cluster=config.cluster,
        taskDefinition=config.task_definition,
        launchType=config.launch_type,
        platformVersion=config.platform_version,
        networkConfiguration=_task_network_config(config),
        overrides=overrides,
        tags=[
            {"key": "supplier_slug", "value": supplier_slug},
            {"key": "job_type", "value": job_type},
        ],
    )
    failures = response.get("failures", [])
    if failures:
        raise EcsJobError(f"Failed to launch ECS task: {failures[0].get('reason', failures[0])}")
    task = response["tasks"][0]
    task_arn = task["taskArn"]
    artifact_prefix = "/".join(part for part in [config.artifact_prefix.strip("/"), supplier_slug, job_type] if part)
    return EcsTaskLaunchResult(
        task_arn=task_arn,
        last_status=task.get("lastStatus", "PROVISIONING"),
        cluster_arn=response.get("clusterArn", ""),
        log_group="",
        log_stream="",
        artifact_prefix=artifact_prefix,
    )


def describe_ecs_task(config: EcsBackendConfig, *, task_arn: str) -> EcsTaskFinalResult:
    client = _client(config.region)
    response = client.describe_tasks(cluster=config.cluster, tasks=[task_arn])
    failures = response.get("failures", [])
    if failures:
        raise EcsJobError(f"Failed to describe ECS task: {failures[0].get('reason', failures[0])}")
    task = response["tasks"][0]
    container = next(
        (
            item
            for item in task.get("containers", [])
            if item.get("name") == config.container_name
        ),
        task.get("containers", [{}])[0],
    )
    exit_code = int(container.get("exitCode", 1)) if container.get("exitCode") is not None else 1
    return EcsTaskFinalResult(
        stop_code=task.get("stopCode", ""),
        stopped_reason=task.get("stoppedReason", ""),
        exit_code=exit_code,
        last_status=task.get("lastStatus", ""),
        log_group="",
        log_stream="",
    )
