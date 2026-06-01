from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from models import CSV_COLUMNS, NormalizedProduct


def record_key(record: NormalizedProduct) -> str:
    if record.sku:
        return f"sku:{record.sku}"
    variant_key = json.dumps(record.variant_options, sort_keys=True, ensure_ascii=False)
    return f"url:{record.product_url}|{variant_key}"


def assign_item_ids(records: list[NormalizedProduct], supplier_slug: str) -> dict[str, str]:
    assigned: dict[str, str] = {}
    missing_counter = 1
    for record in sorted(records, key=lambda item: (item.sku, item.product_url, item.item_name)):
        key = record_key(record)
        if record.sku:
            assigned[key] = record.sku
            continue
        assigned[key] = f"{supplier_slug.upper()}_NO_ID_{missing_counter:06d}"
        missing_counter += 1
    return assigned


def record_to_csv_row(record: NormalizedProduct, item_id: str) -> dict[str, str]:
    row = {column: "" for column in CSV_COLUMNS}
    row["Category name"] = record.category_path or "Uncategorized"
    row["Category ID"] = ""
    row["Item ID"] = item_id
    row["Item name"] = record.item_name
    row["Order by"] = record.order_by or "vessel"
    row["Vessel size"] = record.vessel_size
    row["Vessel unit"] = record.vessel_unit
    row["Vessel type"] = record.vessel_type
    row["Bundle size"] = record.bundle_size
    row["Bundle type"] = record.bundle_type if record.bundle_size else ""
    row["Bundle GTIN"] = record.bundle_gtin
    row["Price"] = record.price
    row["Price per"] = record.price_per or "vessel"
    row["Minimum order count"] = record.min_order_count or "1"
    row["Status"] = record.status
    row["Image"] = record.image_url
    row["GTIN"] = record.gtin
    row["Labels"] = ",".join(record.labels)
    row["Description"] = record.description
    row["Manufacturer"] = record.manufacturer
    row["Brand"] = record.brand
    row["Region"] = record.region
    row["Country"] = record.country
    row["Color"] = record.color
    row["Material"] = record.material
    row["Length"] = record.length
    row["Width"] = record.width
    row["Height"] = record.height
    row["Diameter"] = record.diameter
    row["Net weight"] = record.net_weight
    row["Total weight"] = record.total_weight
    row["VAT"] = record.vat
    row["Product Sheet"] = record.product_sheet_url
    return row


def export_raw_jsonl(records: list[NormalizedProduct], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def export_failures_jsonl(failures: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for failure in failures:
            handle.write(json.dumps(failure, ensure_ascii=False) + "\n")


def build_csv_rows(records: list[NormalizedProduct], supplier_slug: str) -> list[dict[str, str]]:
    item_ids = assign_item_ids(records, supplier_slug)
    return [record_to_csv_row(record, item_ids[record_key(record)]) for record in records]


def build_item_id_map(records: list[NormalizedProduct], supplier_slug: str) -> dict[str, str]:
    return assign_item_ids(records, supplier_slug)


def write_csv_rows(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def export_csv(records: list[NormalizedProduct], output_path: Path, supplier_slug: str) -> list[dict[str, str]]:
    rows = build_csv_rows(records, supplier_slug)
    write_csv_rows(rows, output_path)
    return rows
