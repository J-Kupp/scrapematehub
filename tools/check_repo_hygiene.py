#!/usr/bin/env python3
"""Fail fast when tracked repository contents violate maintenance rules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DOCUMENTS = {
    "docs/architecture.md",
    "docs/new-supplier.md",
    "docs/aws-runbook.md",
    "docs/deployment-and-rollback.md",
    "docs/troubleshooting.md",
}
FORBIDDEN_PATH_PARTS = {"__pycache__", ".venv", ".cache", ".playwright-browsers"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".pem"}
FORBIDDEN_FILENAMES = {".DS_Store", ".env.local"}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def check_repository() -> list[str]:
    errors: list[str] = []
    tracked = tracked_files()
    tracked_set = set(tracked)

    for document in sorted(REQUIRED_DOCUMENTS):
        if document not in tracked_set:
            errors.append(f"Missing required runbook: {document}")

    for raw_path in tracked:
        path = Path(raw_path)
        if path.name in FORBIDDEN_FILENAMES:
            errors.append(f"Generated or secret local file is tracked: {raw_path}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"Sensitive or generated file type is tracked: {raw_path}")
        if any(part in FORBIDDEN_PATH_PARTS for part in path.parts):
            errors.append(f"Generated directory content is tracked: {raw_path}")

    return errors


def main() -> int:
    errors = check_repository()
    if not errors:
        print("Repository hygiene passed.")
        return 0
    print("Repository hygiene failed:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
