from __future__ import annotations

import json
import os


def load_shared_secrets(*, overwrite: bool = False) -> None:
    backend = os.environ.get("SHARED_SECRETS_BACKEND", "").strip().lower()
    if not backend:
        return
    if backend != "aws-secrets-manager":
        raise ValueError(f"Unsupported shared secrets backend: {backend}")
    secret_id = os.environ.get("AWS_SECRETS_MANAGER_SECRET_ID", "").strip()
    if not secret_id:
        raise ValueError("AWS_SECRETS_MANAGER_SECRET_ID must be set for aws-secrets-manager.")

    try:
        import boto3  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is required for AWS Secrets Manager integration."
        ) from exc

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", region_name=region or None)
    response = client.get_secret_value(SecretId=secret_id)
    secret_string = response.get("SecretString", "")
    if not secret_string:
        return
    payload = json.loads(secret_string)
    if not isinstance(payload, dict):
        raise ValueError("AWS Secrets Manager payload must be a JSON object.")
    for key, value in payload.items():
        if key and value is not None and (overwrite or key not in os.environ):
            os.environ[key] = str(value)
