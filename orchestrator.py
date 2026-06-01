from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapters import get_adapter
from core.cleaning import clean_rows, write_correction_report
from core.contracts import NormalizedProduct, SupplierScrapeResult
from core.exporting import (
    build_csv_rows,
    build_item_id_map,
    export_failures_jsonl,
    export_raw_jsonl,
    record_key,
    write_csv_rows,
)
from core.sync import YbmApiError, sync_rows_to_ybm
from core.validation import validate_rows
from config import PROJECT_ROOT, get_log_root, get_state_root, get_supplier_config, load_env_file, load_supplier_configs
from models import SupplierRunResult, SupplierRunState
from packaging import interpret_products, write_packaging_audit



def service_log_path() -> Path:
    return get_log_root() / "service.log"


def setup_service_logger() -> logging.Logger:
    log_path = service_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("supplier_service")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def compute_products_checksum(products: list[NormalizedProduct]) -> str:
    ordered_payload = [
        product.to_dict()
        for product in sorted(products, key=lambda item: (item.sku, item.canonical_url, item.item_name))
    ]
    encoded = json.dumps(ordered_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def supplier_paths(supplier_slug: str, output_dir: Path) -> dict[str, Path]:
    state_dir = get_state_root() / supplier_slug
    state_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return {
        "output_dir": output_dir,
        "csv": output_dir / f"{supplier_slug}_products.csv",
        "correction_report": output_dir / f"{supplier_slug}_corrections.csv",
        "packaging_audit": output_dir / f"{supplier_slug}_packaging_audit.csv",
        "raw_jsonl": output_dir / f"{supplier_slug}_products_raw.jsonl",
        "failures_jsonl": output_dir / f"{supplier_slug}_products_failures.jsonl",
        "listing_diagnostics": output_dir / f"{supplier_slug}_listing_diagnostics.jsonl",
        "run_summary": output_dir / f"{supplier_slug}_run_summary.json",
        "sync_report": output_dir / f"{supplier_slug}_sync_report.json",
        "state_file": state_dir / "state.json",
        "remote_products_snapshot": state_dir / "remote_products.json",
        "remote_categories_snapshot": state_dir / "remote_categories.json",
    }


def load_state(state_file: Path, supplier_slug: str) -> SupplierRunState:
    if not state_file.exists():
        return SupplierRunState(supplier_slug=supplier_slug)
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    return SupplierRunState.from_dict(payload)


def save_state(state: SupplierRunState, state_file: Path) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_products_from_jsonl(path: Path) -> list[NormalizedProduct]:
    products: list[NormalizedProduct] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        products.append(NormalizedProduct.from_dict(json.loads(line)))
    return products


def write_listing_diagnostics(path: Path, diagnostics: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in diagnostics:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_run_summary(
    run_result: SupplierRunResult,
    *,
    dry_run: bool,
    sync_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "supplier_slug": run_result.supplier_slug,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product_count": len(run_result.products),
        "failure_count": len(run_result.failures),
        "discovered_product_url_count": len(run_result.discovered_product_urls),
        "covered_product_url_count": run_result.covered_product_url_count,
        "raw_record_count": run_result.raw_record_count,
        "interpreted_record_count": run_result.interpreted_record_count,
        "checksum": run_result.checksum,
        "validation": {
            "errors": run_result.validation.errors,
            "warnings": run_result.validation.warnings,
        },
        "dry_run": dry_run,
        "output_paths": run_result.output_paths,
        "sync_summary": sync_summary,
    }


def export_and_validate(
    supplier_slug: str,
    products: list[NormalizedProduct],
    failures: list[dict[str, str]],
    discovered_product_urls: set[str],
    listing_diagnostics: list[dict[str, str]],
    paths: dict[str, Path],
    covered_product_url_count: int,
) -> tuple[SupplierRunResult, list[dict[str, str]], list[dict[str, str]]]:
    products.sort(key=lambda item: (item.category_path, item.item_name, item.sku or item.canonical_url))
    item_ids = build_item_id_map(products, supplier_slug)
    audit_rows = interpret_products(products, item_ids, [record_key(product) for product in products])
    export_raw_jsonl(products, paths["raw_jsonl"])
    export_failures_jsonl(failures, paths["failures_jsonl"])
    raw_rows = build_csv_rows(products, supplier_slug)
    rows, correction_rows = clean_rows(raw_rows)
    write_csv_rows(rows, paths["csv"])
    write_correction_report(correction_rows, paths["correction_report"])
    write_packaging_audit(audit_rows, paths["packaging_audit"])
    validation = validate_rows(
        rows,
        discovered_product_urls,
        failures,
        covered_product_url_count=covered_product_url_count,
        products=products,
    )
    write_listing_diagnostics(paths["listing_diagnostics"], listing_diagnostics)
    return SupplierRunResult(
        supplier_slug=supplier_slug,
        products=products,
        failures=failures,
        discovered_product_urls=discovered_product_urls,
        listing_diagnostics=listing_diagnostics,
        validation=validation,
        output_paths={key: str(value) for key, value in paths.items() if key not in {"state_file", "output_dir"}},
        checksum=compute_products_checksum(products),
        covered_product_url_count=covered_product_url_count,
        raw_record_count=0,
        interpreted_record_count=0,
    ), rows, correction_rows


async def scrape_supplier(
    config,
    *,
    force_refresh: bool = False,
) -> SupplierScrapeResult:
    adapter_cls = get_adapter(config.scraper_adapter)
    adapter = adapter_cls(config, PROJECT_ROOT)
    return await adapter.scrape(force_refresh=force_refresh)


def run_supplier(
    supplier_slug: str,
    *,
    dry_run: bool = False,
    force_refresh: bool = False,
    sync_from_export: bool = False,
    env_path: Path | None = None,
    limit_products: int | None = None,
    skip_inactivate: bool = False,
) -> SupplierRunResult:
    service_logger = setup_service_logger()
    load_env_file(env_path)
    config = get_supplier_config(supplier_slug)
    paths = supplier_paths(supplier_slug, config.output_path(PROJECT_ROOT))
    state = load_state(paths["state_file"], supplier_slug)

    if sync_from_export:
        products = load_products_from_jsonl(paths["raw_jsonl"])
        failures: list[dict[str, str]] = []
        discovered_product_urls = {product.product_url for product in products}
        listing_diagnostics: list[dict[str, str]] = []
        covered_product_url_count = len(products)
        raw_record_count = len(products)
        interpreted_record_count = len(products)
    else:
        scrape_result = asyncio.run(
            scrape_supplier(config, force_refresh=force_refresh)
        )
        products = scrape_result.products
        failures = scrape_result.failures
        discovered_product_urls = scrape_result.discovered_product_urls
        listing_diagnostics = scrape_result.listing_diagnostics
        covered_product_url_count = scrape_result.covered_product_url_count
        raw_record_count = scrape_result.raw_record_count
        interpreted_record_count = scrape_result.interpreted_record_count

    run_result, cleaned_rows, correction_rows = export_and_validate(
        supplier_slug,
        products,
        failures,
        discovered_product_urls,
        listing_diagnostics,
        paths,
        covered_product_url_count,
    )
    run_result.raw_record_count = raw_record_count
    run_result.interpreted_record_count = interpreted_record_count

    sync_summary_payload: dict[str, Any] | None = None
    if run_result.validation.is_valid:
        try:
            sync_summary, remote_categories, remote_products = sync_rows_to_ybm(
                config,
                cleaned_rows,
                dry_run=dry_run,
                limit_products=limit_products,
                skip_inactivate=skip_inactivate,
            )
        except YbmApiError as exc:
            sync_summary_payload = {"aborted": True, "reason": "ybm_api_error", "error": str(exc)}
            write_json(paths["sync_report"], sync_summary_payload)
            run_summary = build_run_summary(run_result, dry_run=dry_run, sync_summary=sync_summary_payload)
            write_json(paths["run_summary"], run_summary)
            service_logger.error("Supplier %s sync failed: %s", supplier_slug, exc)
            raise
        sync_summary_payload = sync_summary.to_dict()
        write_json(paths["sync_report"], sync_summary_payload)
        write_json(paths["remote_categories_snapshot"], remote_categories)
        write_json(paths["remote_products_snapshot"], remote_products)
        if not dry_run and not sync_summary.errors:
            state.last_successful_run_at = datetime.now(timezone.utc).isoformat()
            state.last_successful_checksum = run_result.checksum
            state.last_sync_summary = sync_summary_payload
            state.last_export_paths = run_result.output_paths
            state.last_remote_snapshot_path = str(paths["remote_products_snapshot"])
            save_state(state, paths["state_file"])
    else:
        sync_summary_payload = {"aborted": True, "reason": "validation_errors"}
        write_json(paths["sync_report"], sync_summary_payload)

    run_summary = build_run_summary(run_result, dry_run=dry_run, sync_summary=sync_summary_payload)
    run_summary["correction_count"] = len(correction_rows)
    write_json(paths["run_summary"], run_summary)
    if run_result.validation.errors:
        service_logger.error("Supplier %s failed validation: %s", supplier_slug, "; ".join(run_result.validation.errors))
        raise RuntimeError(f"Validation failed for {supplier_slug}: {'; '.join(run_result.validation.errors)}")
    service_logger.info("Supplier %s completed. dry_run=%s products=%s", supplier_slug, dry_run, len(products))
    return run_result


def run_all_suppliers(*, dry_run: bool = False, force_refresh: bool = False, env_path: Path | None = None) -> list[SupplierRunResult]:
    results: list[SupplierRunResult] = []
    for config in load_supplier_configs():
        if not config.enabled:
            continue
        results.append(
            run_supplier(
                config.supplier_slug,
                dry_run=dry_run,
                force_refresh=force_refresh,
                sync_from_export=False,
                env_path=env_path,
                limit_products=None,
                skip_inactivate=False,
            )
        )
    return results
