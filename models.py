from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
from typing import Any


CSV_COLUMNS = [
    "Category name",
    "Category ID",
    "Item ID",
    "Item name",
    "Order by",
    "Vessel size",
    "Vessel unit",
    "Vessel type",
    "Bundle size",
    "Bundle type",
    "Bundle GTIN",
    "Price",
    "Price per",
    "Minimum order count",
    "Status",
    "Image",
    "GTIN",
    "Labels",
    "Description",
    "Manufacturer",
    "Brand",
    "Region",
    "Country",
    "Vintage",
    "Ingredients",
    "Allergens",
    "Storage advice",
    "Nutritional values",
    "Dietary labels",
    "Alcohol content",
    "Color",
    "Grape variety",
    "Wine-making",
    "Material",
    "Fishing method",
    "Length",
    "Width",
    "Height",
    "Diameter",
    "Net weight",
    "Total weight",
    "VAT",
    "Product Sheet",
    "Name FR",
    "Name IT",
    "Name EN",
]


@dataclass
class NormalizedProduct:
    product_url: str
    canonical_url: str
    category_path: str
    product_name: str
    item_name: str
    sku: str = ""
    variant_name: str = ""
    variant_options: dict[str, str] = field(default_factory=dict)
    gtin: str = ""
    bundle_gtin: str = ""
    price: str = ""
    currency: str = ""
    vat: str = ""
    price_per: str = "vessel"
    order_by: str = "vessel"
    min_order_count: str = "1"
    status: str = "ACTIVE"
    image_url: str = ""
    product_sheet_url: str = ""
    description: str = ""
    manufacturer: str = ""
    brand: str = ""
    region: str = ""
    country: str = ""
    labels: list[str] = field(default_factory=list)
    vessel_size: str = ""
    vessel_unit: str = ""
    vessel_type: str = ""
    bundle_size: str = ""
    bundle_type: str = ""
    raw_bundle_text: str = ""
    raw_detail_price_unit_text: str = ""
    raw_spec_piece_text: str = ""
    raw_fill_text: str = ""
    packaging_mode: str = ""
    packaging_evidence: str = ""
    packaging_source: str = ""
    color: str = ""
    material: str = ""
    length: str = ""
    width: str = ""
    height: str = ""
    diameter: str = ""
    net_weight: str = ""
    total_weight: str = ""
    raw_availability_text: str = ""
    specs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NormalizedProduct":
        return cls(**payload)


@dataclass
class SupplierConfig:
    supplier_slug: str
    enabled: bool
    scraper_adapter: str
    base_url: str
    ybm_token_env_var: str
    output_dir: str
    catalog_update_policy: str = "delete_missing"
    schedule: dict[str, str] = field(default_factory=dict)
    ybm_api_base: str = "https://connect.yourbarmate.com/api"
    scrape_settings: dict[str, Any] = field(default_factory=dict)
    archived: bool = False
    alert_settings: dict[str, Any] = field(default_factory=dict)

    def output_path(self, project_root: Path) -> Path:
        path = Path(self.output_dir)
        if not path.is_absolute():
            output_root = os.environ.get("SUPPLIER_OUTPUT_ROOT", "").strip()
            if output_root:
                path = Path(output_root).expanduser() / path
            else:
                path = project_root / path
        return path


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    performed: bool = True
    row_count: int = 0
    passed_row_count: int = 0

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass
class SupplierRunState:
    supplier_slug: str
    last_successful_run_at: str = ""
    last_successful_checksum: str = ""
    last_sync_summary: dict[str, Any] = field(default_factory=dict)
    last_export_paths: dict[str, str] = field(default_factory=dict)
    last_remote_snapshot_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SupplierRunState":
        return cls(**payload)


@dataclass
class SupplierRunResult:
    supplier_slug: str
    products: list[NormalizedProduct]
    failures: list[dict[str, str]]
    discovered_product_urls: set[str]
    listing_diagnostics: list[dict[str, str]]
    validation: ValidationResult
    output_paths: dict[str, str]
    checksum: str
    covered_product_url_count: int
    raw_record_count: int = 0
    interpreted_record_count: int = 0
    enrichment_failures: list[dict[str, str]] = field(default_factory=list)


@dataclass
class SyncSummary:
    supplier_slug: str
    dry_run: bool
    old_catalog_products: int = 0
    created_categories: int = 0
    updated_categories: int = 0
    created_products: int = 0
    updated_products: int = 0
    inactivated_products: int = 0
    unchanged_products: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
