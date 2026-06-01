from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models import NormalizedProduct


@dataclass
class RawSupplierRecord:
    source_url: str
    payload: dict[str, Any]


@dataclass
class SupplierInterpretedRecord:
    source_url: str
    data: dict[str, Any]
    notes: list[str] = field(default_factory=list)


@dataclass
class SupplierScrapeResult:
    products: list[NormalizedProduct]
    failures: list[dict[str, str]]
    discovered_product_urls: set[str]
    listing_diagnostics: list[dict[str, str]]
    covered_product_url_count: int
    raw_record_count: int = 0
    interpreted_record_count: int = 0
