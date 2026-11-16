from __future__ import annotations

import re
from html import unescape
from typing import Any
from models import NormalizedProduct


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def product_identifier(record: dict[str, Any]) -> str:
    return normalize_space(record.get("itemNumber") or record.get("id"))


def product_url(base_url: str, item_number: str) -> str:
    return f"{base_url.rstrip('/')}/portal/app/catalog/category/product/{item_number}"


def discover_product_urls(payload: dict[str, Any], base_url: str) -> list[str]:
    """Read product identifiers from one public catalog search page."""
    records = payload.get("content") or []
    urls = {
        product_url(base_url, item_number)
        for record in records
        if isinstance(record, dict) and (item_number := product_identifier(record))
    }
    return sorted(urls)


def _attribute(master: dict[str, Any], *names: str) -> str:
    attributes = master.get("attributes") or {}
    for name in names:
        value = attributes.get(name)
        if isinstance(value, dict):
            result = normalize_space(value.get("value"))
            if result:
                return result
    return ""


def _category_path(categories: list[dict[str, Any]]) -> str:
    ordered = sorted(
        (item for item in categories if isinstance(item, dict) and normalize_space(item.get("name"))),
        key=lambda item: int(item.get("level", 0)),
    )
    return " > ".join(dict.fromkeys(normalize_space(item.get("name")) for item in ordered))


def _selected_unit(master: dict[str, Any]) -> dict[str, Any]:
    units = [unit for unit in (master.get("units") or []) if isinstance(unit, dict)]
    base_code = normalize_space(master.get("baseOrderingUnit"))
    return next((unit for unit in units if normalize_space(unit.get("unitCode")) == base_code), units[0] if units else {})


def _packaging(unit: dict[str, Any]) -> tuple[str, str, str, str]:
    bundle = unit.get("bundleUnit") or {}
    if isinstance(bundle, dict) and normalize_space(bundle.get("quantity")):
        amount = normalize_space(bundle.get("quantity"))
        source_unit = normalize_space(bundle.get("unit")).lower()
    else:
        source_unit = normalize_space(unit.get("unit")).lower()
        amount = normalize_space(unit.get("netWeightKg") or unit.get("grossWeightKg"))
        if source_unit in {"kg", "kilogram", "kilograms"} and amount:
            return amount, "kg", "", ""
        if source_unit in {"g", "gram", "grams"} and amount:
            return amount, "g", "", ""
        if source_unit in {"l", "liter", "litre"} and amount:
            return amount, "l", "", ""
        if source_unit in {"ml", "milliliter", "millilitre"} and amount:
            return amount, "ml", "", ""
        return ("1", "quantity", "", "") if source_unit else ("", "", "", "")

    unit_aliases = {
        "kilogram": "kg", "kilograms": "kg", "gram": "g", "grams": "g",
        "liter": "l", "litre": "l", "milliliter": "ml", "millilitre": "ml",
        "stück": "quantity", "stuck": "quantity", "piece": "quantity", "pieces": "quantity",
    }
    vessel_unit = unit_aliases.get(source_unit, source_unit)
    if vessel_unit not in {"kg", "g", "l", "dl", "cl", "ml", "quantity"}:
        return "1", "quantity", "", ""
    return amount, vessel_unit, "", ""


def _image_url(master: dict[str, Any]) -> str:
    images = master.get("images") or master.get("assets") or []
    for image in images:
        if not isinstance(image, dict):
            continue
        sources = image.get("sources") or []
        for source in sources:
            if isinstance(source, dict) and source.get("size") == "large" and source.get("url"):
                return str(source["url"])
        if sources and isinstance(sources[0], dict) and sources[0].get("url"):
            return str(sources[0]["url"])
    return ""


def _html_text(value: Any) -> str:
    value = unescape(str(value or ""))
    return normalize_space(re.sub(r"<[^>]+>", " ", value))


def parse_product_record(
    payload: dict[str, Any],
    base_url: str,
    listing_record: dict[str, Any] | None = None,
) -> NormalizedProduct | None:
    master = payload.get("master") or {}
    item_number = normalize_space(payload.get("itemNumber") or master.get("sku"))
    name = normalize_space(payload.get("name") or master.get("name"))
    if not item_number or not name:
        return None

    canonical = product_url(base_url, item_number)
    unit = _selected_unit(master)
    vessel_size, vessel_unit, _, _ = _packaging(unit)
    attributes = master.get("attributes") or {}
    labels = attributes.get("labels") or {}
    label_values = (labels.get("values") or []) if isinstance(labels, dict) else []
    if not label_values and isinstance(labels, dict) and labels.get("value"):
        label_values = [labels["value"]]
    category_path = normalize_space((listing_record or {}).get("category")) or _category_path(payload.get("categories") or [])
    raw_price = unit.get("price") or master.get("price")
    if isinstance(raw_price, dict):
        raw_price = raw_price.get("amount") or raw_price.get("value") or ""

    specs = {
        "base_ordering_unit": normalize_space(master.get("baseOrderingUnit")),
        "unit_label": normalize_space(unit.get("unit")),
        "bundle_unit": normalize_space((unit.get("bundleUnit") or {}).get("unit")) if isinstance(unit.get("bundleUnit"), dict) else "",
        "bundle_quantity": normalize_space((unit.get("bundleUnit") or {}).get("quantity")) if isinstance(unit.get("bundleUnit"), dict) else "",
        "delivery_group": normalize_space(master.get("deliveryGroup")),
        "availability": normalize_space((master.get("availability") or {}).get("available")),
    }
    specs = {key: value for key, value in specs.items() if value}

    return NormalizedProduct(
        product_url=canonical,
        canonical_url=canonical,
        category_path=category_path,
        product_name=name,
        item_name=name,
        sku=item_number,
        gtin=_attribute(master, "selling_unit_1_gtin", "gtin"),
        price=normalize_space(raw_price),
        currency="CHF",
        status="ACTIVE" if normalize_space(payload.get("status")).lower() in {"", "active"} else "INACTIVE",
        image_url=_image_url(master),
        description=_html_text(payload.get("description") or payload.get("shortDescription")),
        ingredients=_html_text(_attribute(master, "ehs_ingredients", "ingredients")),
        allergens=_attribute(master, "contains_allergenes", "allergens"),
        storage_advice=_attribute(master, "ehs_storage_advice", "storage_advice_s1"),
        manufacturer=normalize_space(master.get("manufacturer")),
        country=_attribute(master, "country_of_origin", "country_of_manufacture"),
        labels=[normalize_space(value) for value in label_values if normalize_space(value)],
        dietary_labels=", ".join(normalize_space(value) for value in label_values if normalize_space(value)),
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        net_weight=normalize_space(unit.get("netWeightKg")),
        total_weight=normalize_space(unit.get("grossWeightKg")),
        raw_bundle_text=normalize_space(unit.get("unit")),
        packaging_evidence=normalize_space(unit.get("unit")),
        packaging_source="merat_catalog_api",
        raw_detail_price_unit_text=normalize_space(unit.get("unit")),
        specs=specs,
    )


__all__ = ["discover_product_urls", "parse_product_record", "product_identifier", "product_url"]
