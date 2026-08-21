#!/usr/bin/env python3
"""Keep live dashboard supplier settings while importing newly deployed suppliers."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"suppliers": []}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid supplier config JSON: {path}") from exc

    suppliers = payload.get("suppliers")
    if not isinstance(suppliers, list):
        raise ValueError(f"Supplier config must contain a suppliers list: {path}")
    return payload


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as temporary_file:
        json.dump(payload, temporary_file, indent=2)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, path)


def merge_supplier_configs(
    *,
    defaults_path: Path,
    runtime_path: Path,
    legacy_path: Path,
) -> tuple[int, int]:
    """Return the number of preserved and newly imported supplier definitions."""
    defaults = load_payload(defaults_path)
    if runtime_path.exists():
        runtime = load_payload(runtime_path)
    elif legacy_path.exists():
        # One-time migration from the old, deployment-managed config location.
        runtime = load_payload(legacy_path)
    else:
        runtime = {"suppliers": []}

    runtime_suppliers = runtime["suppliers"]
    existing_slugs = {
        supplier.get("supplier_slug")
        for supplier in runtime_suppliers
        if isinstance(supplier, dict) and supplier.get("supplier_slug")
    }
    added = 0
    for supplier in defaults["suppliers"]:
        if not isinstance(supplier, dict):
            raise ValueError(f"Supplier entries must be objects: {defaults_path}")
        slug = supplier.get("supplier_slug")
        if not slug:
            raise ValueError(f"Supplier entry is missing supplier_slug: {defaults_path}")
        if slug not in existing_slugs:
            runtime_suppliers.append(supplier)
            existing_slugs.add(slug)
            added += 1

    write_payload(runtime_path, runtime)
    return len(runtime_suppliers) - added, added


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--defaults", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--legacy", required=True, type=Path)
    args = parser.parse_args()

    preserved, added = merge_supplier_configs(
        defaults_path=args.defaults,
        runtime_path=args.runtime,
        legacy_path=args.legacy,
    )
    print(f"Supplier runtime config ready: preserved={preserved}, added_from_git={added}")


if __name__ == "__main__":
    main()
