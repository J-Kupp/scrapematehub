from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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


def _logs_client(region: str):
    try:
        import boto3  # type: ignore
    except ImportError as exc:
        raise EcsJobError("boto3 is required for the ECS Fargate backend.") from exc
    return boto3.client("logs", region_name=region or None)


def _container_log_destination(
    container_definition: dict[str, Any],
    *,
    container_name: str,
    task_arn: str,
) -> tuple[str, str]:
    log_config = container_definition.get("logConfiguration", {})
    options = log_config.get("options", {})
    log_group = options.get("awslogs-group", "")
    stream_prefix = options.get("awslogs-stream-prefix", "")
    task_id = task_arn.rsplit("/", 1)[-1] if task_arn else ""
    if not log_group or not stream_prefix or not task_id:
        return log_group, ""
    return log_group, f"{stream_prefix}/{container_name}/{task_id}"


def resolve_log_destination(
    config: EcsBackendConfig,
    *,
    task_arn: str,
    task_definition: str = "",
) -> tuple[str, str]:
    client = _client(config.region)
    response = client.describe_task_definition(
        taskDefinition=task_definition or config.task_definition
    )
    container_definition = next(
        (
            item
            for item in response["taskDefinition"].get("containerDefinitions", [])
            if item.get("name") == config.container_name
        ),
        response["taskDefinition"].get("containerDefinitions", [{}])[0],
    )
    return _container_log_destination(
        container_definition,
        container_name=config.container_name,
        task_arn=task_arn,
    )


def read_ecs_task_logs(
    config: EcsBackendConfig,
    *,
    task_arn: str,
    log_group: str = "",
    log_stream: str = "",
    limit: int = 200,
) -> tuple[str, str, str]:
    resolved_group = log_group
    resolved_stream = log_stream
    if not resolved_group or not resolved_stream:
        resolved_group, resolved_stream = resolve_log_destination(config, task_arn=task_arn)
    if not resolved_group or not resolved_stream:
        return resolved_group, resolved_stream, ""

    client = _logs_client(config.region)
    try:
        response = client.get_log_events(
            logGroupName=resolved_group,
            logStreamName=resolved_stream,
            startFromHead=False,
            limit=limit,
        )
    except client.exceptions.ResourceNotFoundException:
        return resolved_group, resolved_stream, ""
    events = response.get("events", [])
    lines: list[str] = []
    for event in events:
        timestamp_ms = event.get("timestamp")
        message = str(event.get("message", "")).rstrip()
        if not message:
            continue
        if timestamp_ms is None:
            lines.append(message)
            continue
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        lines.append(f"[{timestamp.isoformat()}] {message}")
    return resolved_group, resolved_stream, "\n".join(lines)


def count_ecs_stream_matches(
    config: EcsBackendConfig,
    *,
    task_arn: str,
    log_group: str = "",
    log_stream: str = "",
    needle: str = "Parsed product",
    page_limit: int = 10000,
) -> int:
    resolved_group = log_group
    resolved_stream = log_stream
    if not resolved_group or not resolved_stream:
        resolved_group, resolved_stream = resolve_log_destination(config, task_arn=task_arn)
    if not resolved_group or not resolved_stream:
        return 0

    client = _logs_client(config.region)
    count = 0
    token: str | None = None
    pages = 0
    while pages < page_limit:
        kwargs: dict[str, Any] = {
            "logGroupName": resolved_group,
            "logStreamName": resolved_stream,
            "startFromHead": True,
        }
        if token:
            kwargs["nextToken"] = token
        response = client.get_log_events(**kwargs)
        events = response.get("events", [])
        for event in events:
            message = str(event.get("message", ""))
            if needle in message:
                count += 1
        next_token = response.get("nextForwardToken")
        pages += 1
        if not next_token or next_token == token:
            break
        token = next_token
    return count


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
    log_group, log_stream = resolve_log_destination(
        config,
        task_arn=task_arn,
        task_definition=task.get("taskDefinitionArn", ""),
    )
    artifact_prefix = "/".join(part for part in [config.artifact_prefix.strip("/"), supplier_slug, job_type] if part)
    return EcsTaskLaunchResult(
        task_arn=task_arn,
        last_status=task.get("lastStatus", "PROVISIONING"),
        cluster_arn=response.get("clusterArn", ""),
        log_group=log_group,
        log_stream=log_stream,
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
    task_definition = task.get("taskDefinitionArn", config.task_definition)
    log_group, log_stream = resolve_log_destination(
        config,
        task_arn=task_arn,
        task_definition=task_definition,
    )
    exit_code = int(container.get("exitCode", 1)) if container.get("exitCode") is not None else 1
    return EcsTaskFinalResult(
        stop_code=task.get("stopCode", ""),
        stopped_reason=task.get("stoppedReason", ""),
        exit_code=exit_code,
        last_status=task.get("lastStatus", ""),
        log_group=log_group,
        log_stream=log_stream,
    )


def stop_ecs_task(config: EcsBackendConfig, *, task_arn: str, reason: str = "") -> None:
    client = _client(config.region)
    kwargs: dict[str, Any] = {
        "cluster": config.cluster,
        "task": task_arn,
    }
    if reason.strip():
        kwargs["reason"] = reason.strip()
    client.stop_task(**kwargs)
