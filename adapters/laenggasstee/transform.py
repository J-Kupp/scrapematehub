from __future__ import annotations

import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from models import NormalizedProduct


PACKAGING_RE = re.compile(r"(?P<value>\d[\d.,]*)\s*(?P<unit>kg|g|ml|cl|dl|l|stk|st)\b", re.IGNORECASE)


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def normalize_number(value: Any) -> str:
    text = normalize_space(value).replace("'", "").replace(",", ".")
    if not text:
        return ""
    try:
        return f"{float(text):.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return text


def normalize_unit(value: str) -> str:
    unit = normalize_space(value).lower()
    return "st" if unit in {"stk", "stück", "stueck"} else unit


def strip_html(value: Any) -> str:
    return normalize_space(BeautifulSoup(unescape(str(value or "")), "lxml").get_text(" ", strip=True))


def product_url(base_url: str, product_id: str) -> str:
    return f"{base_url.rstrip('/')}/shop/detail/{product_id}"


def _translated_value(payload: dict[str, Any], key: str) -> Any:
    translated = payload.get("translated")
    if isinstance(translated, dict) and translated.get(key) not in (None, ""):
        return translated[key]
    return payload.get(key)


def _category_path(product: dict[str, Any]) -> str:
    candidates = [product.get("seoCategory"), *list(product.get("categories") or [])]
    extension = (product.get("extensions") or {}).get("teaProductExtension") or {}
    candidates.append(extension.get("displayCategory") if isinstance(extension, dict) else None)
    for category in candidates:
        if not isinstance(category, dict):
            continue
        breadcrumb = _translated_value(category, "breadcrumb") or category.get("breadcrumb")
        if isinstance(breadcrumb, list):
            parts = [normalize_space(part) for part in breadcrumb if normalize_space(part)]
            if parts and parts[0] == "Länggass-Tee":
                parts = parts[1:]
            if parts:
                return " > ".join(parts)
    return "Camellia sinensis"


def _image_url(product: dict[str, Any]) -> str:
    cover = product.get("cover") or {}
    media = cover.get("media") if isinstance(cover, dict) else None
    if isinstance(media, dict) and normalize_space(media.get("url")):
        return normalize_space(media["url"])
    for entry in product.get("media") or []:
        media = entry.get("media") if isinstance(entry, dict) else None
        if isinstance(media, dict) and normalize_space(media.get("url")):
            return normalize_space(media["url"])
    return ""


def _packaging(product: dict[str, Any]) -> tuple[str, str, str]:
    raw = normalize_space(_translated_value(product, "packUnit"))
    match = PACKAGING_RE.search(raw)
    if match:
        return normalize_number(match.group("value")), normalize_unit(match.group("unit")), raw

    custom_fields = product.get("customFields") or {}
    grams = custom_fields.get("product_detail_custom_fields_weight_in_gram")
    if grams not in (None, ""):
        return normalize_number(grams), "g", raw

    weight = product.get("weight")
    if weight not in (None, "", 0):
        return normalize_number(weight), "kg", raw
    return "", "", raw


def _property_labels(product: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for prop in product.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        name = normalize_space(_translated_value(prop, "name"))
        if name and name not in labels:
            labels.append(name)
    return labels


def _tea_specs(product: dict[str, Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    extension = (product.get("extensions") or {}).get("teaProductExtension") or {}
    if isinstance(extension, dict):
        for key, value in extension.items():
            if key in {"translated", "displayCategory", "apiAlias", "createdAt", "updatedAt"}:
                continue
            if isinstance(value, (str, int, float)) and normalize_space(value):
                specs[f"tea_{key}"] = normalize_space(value)
    custom_fields = product.get("customFields") or {}
    for key, value in custom_fields.items():
        if isinstance(value, (str, int, float)) and normalize_space(value):
            specs[f"custom_{key}"] = normalize_space(value)
    return specs


def parse_product_record(product: dict[str, Any], base_url: str) -> NormalizedProduct | None:
    product_id = normalize_space(product.get("id"))
    name = normalize_space(_translated_value(product, "name"))
    if not product_id or not name:
        return None

    calculated_price = product.get("calculatedPrice") or {}
    tax = product.get("tax") or {}
    extension = (product.get("extensions") or {}).get("teaProductExtension") or {}
    manufacturer = product.get("manufacturer") or {}
    vessel_size, vessel_unit, raw_packaging = _packaging(product)
    specs = _tea_specs(product)
    specs["shopware_product_id"] = product_id
    specs["stock"] = normalize_number(product.get("stock"))
    if raw_packaging:
        specs["pack_unit"] = raw_packaging

    region = normalize_space(extension.get("googleLink")) if isinstance(extension, dict) else ""
    country = region.split(" / ", 1)[0] if region else ""
    status = "ACTIVE" if product.get("active", True) and product.get("available", True) else "INACTIVE"
    return NormalizedProduct(
        product_url=product_url(base_url, product_id),
        canonical_url=product_url(base_url, product_id),
        category_path=_category_path(product),
        product_name=name,
        item_name=name,
        sku=normalize_space(product.get("productNumber")),
        gtin=normalize_space(product.get("ean")),
        bundle_gtin=normalize_space(product.get("ean")),
        price=normalize_number(calculated_price.get("unitPrice")),
        currency="CHF",
        vat=normalize_number(tax.get("taxRate")),
        status=status,
        image_url=_image_url(product),
        description=strip_html(_translated_value(product, "description")),
        manufacturer=normalize_space(_translated_value(manufacturer, "name")),
        brand="Länggass-Tee",
        region=region,
        country=country,
        labels=_property_labels(product),
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        vessel_type="piece" if vessel_unit == "st" else "",
        raw_bundle_text=raw_packaging,
        total_weight=normalize_number(product.get("weight")),
        specs=specs,
    )
