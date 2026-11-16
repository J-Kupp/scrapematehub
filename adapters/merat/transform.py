from __future__ import annotations

import re
from html import unescape
from decimal import Decimal
import json
from urllib.parse import urljoin
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


def _packaging(master: dict[str, Any], unit: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Use the selected sale unit, resolving count bundles through the published inner unit."""
    bundle = unit.get("bundleUnit") or {}
    code = normalize_space(bundle.get("unitCode"))
    quantity = normalize_space(bundle.get("quantity"))
    measures = {"KGM": "kg", "GRM": "g", "LTR": "l", "MLT": "ml", "CLT": "cl", "DLT": "dl"}
    if code in measures and quantity:
        return quantity, measures[code], "", "", ""
    if code == "PCE" and quantity:
        inner = next((u for u in master.get("units") or [] if u.get("unitCode") == code), {})
        weight = normalize_space(inner.get("netWeightKg"))
        if weight and Decimal(weight) > 0 and Decimal(quantity) == Decimal(quantity).to_integral_value():
            return weight, "kg", quantity, {"CT": "CT", "MP": "PK", "SCH": "PU", "301": "BG"}.get(unit.get("unitCode"), "PK"), code
        return quantity, "quantity", "", "", ""
    if unit.get("unitCode") in measures:
        return "1", measures[unit["unitCode"]], "", "", ""
    weight = normalize_space(unit.get("netWeightKg"))
    if weight and Decimal(weight) > 0:
        return weight, "kg", "", "", ""
    return "1", "quantity", "", "", ""


def _nutrition(master: dict[str, Any]) -> str:
    values = []
    for key, attr in (master.get("attributes") or {}).items():
        if key.startswith("ehs_nutrient_") and key != "ehs_nutrient_per" and attr.get("value"):
            values.append(f"{normalize_space(attr.get('label') or key)}: {_html_text(attr['value'])}")
    per = _attribute(master, "ehs_nutrient_per")
    return "; ".join(([f"pro {per}"] if per else []) + values) if values else ""


def _sheet(master: dict[str, Any], base_url: str) -> str:
    for asset in master.get("assets") or []:
        if "fact_sheet" in str(asset.get("set", "")) or "datenblatt" in str(asset.get("title", "")).lower():
            return urljoin(base_url, asset.get("source") or "")
    return ""


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
    vessel_size, vessel_unit, bundle_size, bundle_type, inner_code = _packaging(master, unit)
    attributes = master.get("attributes") or {}
    labels = attributes.get("labels") or {}
    label_values = (labels.get("values") or []) if isinstance(labels, dict) else []
    if not label_values and isinstance(labels, dict) and labels.get("value"):
        label_values = [labels["value"]]
    category_path = normalize_space((listing_record or {}).get("category")) or _category_path(payload.get("categories") or [])
    raw_price = unit.get("price") or master.get("price")
    if isinstance(raw_price, dict):
        raw_price = raw_price.get("amount") or raw_price.get("value") or ""

    nutrition = _nutrition(master)
    sheet = _sheet(master, base_url)
    certifications = [normalize_space(value) for value in label_values if normalize_space(value)]
    diet = [value for value in certifications if value.casefold() in {"vegan", "vegetarisch", "glutenfrei", "laktosefrei"}]
    specs = {
        "base_ordering_unit": normalize_space(master.get("baseOrderingUnit")),
        "unit_label": normalize_space(unit.get("unit")),
        "bundle_unit": normalize_space((unit.get("bundleUnit") or {}).get("unit")) if isinstance(unit.get("bundleUnit"), dict) else "",
        "bundle_quantity": normalize_space((unit.get("bundleUnit") or {}).get("quantity")) if isinstance(unit.get("bundleUnit"), dict) else "",
        "delivery_group": normalize_space(master.get("deliveryGroup")),
        "availability": normalize_space((master.get("availability") or {}).get("available")),
    }
    specs.update({
        "sale_units": json.dumps(master.get("units") or [], ensure_ascii=False),
        "certifications": ", ".join(certifications),
        "origin": _attribute(master, "fishing_zone"),
    })
    specs = {key: value for key, value in specs.items() if value}
    description_parts = [_html_text(payload.get("description") or payload.get("shortDescription") or _attribute(master, "description"))]
    # The current sync API maps Description but not every optional CSV detail field.
    # Keep these details readable in the synced catalog as well as the dedicated export fields.
    for label, value in [
        ("Ernährungsmerkmale", ", ".join(diet)),
        ("Spurenhinweis", _attribute(master, "may_contain_allergenes")),
        ("Transport", _attribute(master, "merat_transport_distribution_center")),
        ("Saison", _attribute(master, "merat_seasons")),
        ("Produktionsland", _attribute(master, "country_of_origin")),
        ("Zertifizierungen", ", ".join(certifications)),
        ("Herkunft", _attribute(master, "fishing_zone")),
        ("Nährwerte", nutrition), ("Produktdatenblatt", sheet),
        ("Französisch", _attribute(master, "pk_name_fr")),
        ("Italienisch", _attribute(master, "pk_name_it")),
        ("Englisch", _attribute(master, "pk_name_en")),
    ]:
        if value:
            description_parts.append(f"{label}: {value}")
    for key, attr in attributes.items():
        group = (attr.get("group") or {}).get("name", "")
        if group in {"origin", "general", "storage"} and attr.get("display") == "product" and attr.get("value"):
            if key not in {"country_of_origin", "fishing_zone", "ehs_storage_advice", "storage_advice_s1"}:
                description_parts.append(f"{normalize_space(attr.get('label') or key)}: {_html_text(attr['value'])}")
    gtin = _attribute(master, f"gtin_{inner_code}", "base_unit_gtin") if inner_code else _attribute(master, "selling_unit_1_gtin", "gtin")
    bundle_gtin = _attribute(master, "selling_unit_1_gtin") if bundle_size else ""
    inner_unit = next((u for u in master.get("units") or [] if u.get("unitCode") == inner_code), unit) if inner_code else unit

    return NormalizedProduct(
        product_url=canonical,
        canonical_url=canonical,
        category_path=category_path,
        product_name=name,
        item_name=name,
        sku=item_number,
        gtin=gtin,
        bundle_gtin=bundle_gtin,
        name_fr=_attribute(master, "pk_name_fr"),
        name_it=_attribute(master, "pk_name_it"),
        name_en=_attribute(master, "pk_name_en"),
        price=normalize_space(raw_price),
        currency="CHF",
        status="ACTIVE" if normalize_space(payload.get("status")).lower() in {"", "active"} else "INACTIVE",
        image_url=_image_url(master),
        description="\n".join(value for value in description_parts if value),
        product_sheet_url=sheet,
        nutritional_values=nutrition,
        ingredients=_html_text(_attribute(master, "ehs_ingredients", "ingredients")),
        allergens=_attribute(master, "contains_allergenes", "allergens"),
        storage_advice=_attribute(master, "ehs_storage_advice", "storage_advice_s1"),
        manufacturer=normalize_space(master.get("manufacturer")),
        country=normalize_space((attributes.get("country_of_origin") or {}).get("valueCode")) or _attribute(master, "country_of_origin", "country_of_manufacture"),
        labels=["BIO"] if any("bio" == value.casefold() for value in certifications) else [],
        dietary_labels=", ".join(diet),
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        bundle_size=bundle_size,
        bundle_type=bundle_type,
        min_order_count=normalize_space(unit.get("minimumQuantity")) or "1",
        packaging_mode="structured_supplier_units",
        net_weight=normalize_space(inner_unit.get("netWeightKg")),
        total_weight=normalize_space(inner_unit.get("grossWeightKg")),
        raw_bundle_text=normalize_space(unit.get("unit")),
        packaging_evidence=normalize_space(unit.get("unit")),
        packaging_source="merat_catalog_api",
        raw_detail_price_unit_text=normalize_space(unit.get("unit")),
        specs=specs,
    )


__all__ = ["discover_product_urls", "parse_product_record", "product_identifier", "product_url"]
