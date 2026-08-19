from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from models import NormalizedProduct


PRODUCT_URL_RE = re.compile(r"^https?://[^/]+/catalog2/products/\d+/[^/?#]+/?$", re.IGNORECASE)
COUNT_UNITS = {"st", "sch", "stk", "bund", "bd", "pack"}
WEIGHT_UNITS = {"g", "kg"}
VOLUME_UNITS = {"ml", "cl", "dl", "l"}
DELIVERY_SIZE_RE = re.compile(
    r"^(?P<container>.+?)\s+à\s+(?P<value>\d[\d.,]*)\s*(?P<unit>[A-Za-zÄÖÜäöüß]+)(?:\s*\((?P<extra>[^)]+)\))?$"
)
WEIGHT_OR_VOLUME_RE = re.compile(r"(?P<value>\d[\d.,]*)\s*(?P<unit>kg|g|ml|cl|dl|l)\b", re.IGNORECASE)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def normalize_number(value: Any) -> str:
    if value is None:
        return ""
    text = normalize_space(str(value)).replace("'", "").replace("’", "").replace(",", ".")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    formatted = f"{number:.4f}".rstrip("0").rstrip(".")
    return formatted or "0"


def normalize_unit(value: str) -> str:
    lowered = normalize_space(value).lower()
    if lowered in {"stk"}:
        return "st"
    return lowered


def normalize_label(value: str) -> str:
    return normalize_space(value).strip(",")


def product_candidate_from_url(url: str) -> bool:
    return bool(PRODUCT_URL_RE.match(url.strip()))


def absolute_url(base_url: str, href: str | None) -> str:
    if not href:
        return ""
    return urljoin(base_url, href)


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def _iter_react_component_payloads(soup: BeautifulSoup) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for script in soup.select('script[data-store="react-components"]'):
        text = script.get_text(strip=True)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            payloads.append(data)
    return payloads


def extract_catalog_categories(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    for payload in _iter_react_component_payloads(soup):
        for component in payload.values():
            if component.get("constructor_type") != "react_components/catalog_menu$default":
                continue
            catalog = component.get("props", {}).get("catalog", {})
            categories: list[dict[str, str]] = []
            for main in catalog.get("children", []):
                if main.get("tag_name") is not None:
                    continue
                main_name = normalize_space(str(main.get("name", "")))
                for child in main.get("children", []):
                    if child.get("tag_name") is not None:
                        continue
                    category_url = absolute_url(base_url, child.get("link"))
                    if not category_url:
                        continue
                    categories.append(
                        {
                            "category_id": str(child.get("catid", "")),
                            "category_name": normalize_space(str(child.get("name", ""))),
                            "category_url": category_url,
                            "parent_category_name": main_name,
                        }
                    )
            return categories
    return []


def extract_product_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = {
        absolute_url(base_url, anchor.get("href"))
        for anchor in soup.select('a[href^="/catalog2/products/"]')
        if product_candidate_from_url(absolute_url(base_url, anchor.get("href")))
    }
    return sorted(urls)


def extract_pagination_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: set[str] = set()
    selectors = [
        ".pager a[href]",
        ".pagination a[href]",
        "[class*='pager'] a[href]",
        "[class*='pagination'] a[href]",
    ]
    for selector in selectors:
        for anchor in soup.select(selector):
            href = absolute_url(base_url, anchor.get("href"))
            if not href or product_candidate_from_url(href):
                continue
            urls.add(href)
    return sorted(urls)


def extract_product_payload(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "lxml")
    for payload in _iter_react_component_payloads(soup):
        for component in payload.values():
            if component.get("constructor_type") != "react_components/detail/detail-intro$default":
                continue
            product = component.get("props", {}).get("product")
            if isinstance(product, dict):
                return product
    return None


def _extract_breadcrumb_path(soup: BeautifulSoup, fallback_product: dict[str, Any]) -> str:
    parts: list[str] = []
    for node in soup.select(".breadcrumb__item"):
        value = normalize_space(node.get_text(" ", strip=True))
        if not value or value == "Zurück":
            continue
        parts.append(value)
    if parts and parts[0] == "Katalog":
        parts = parts[1:]
    product_name = normalize_space(str(fallback_product.get("shorttext", "")))
    if parts and parts[-1] == product_name:
        parts = parts[:-1]
    if parts:
        return " > ".join(parts)

    fallback_parts: list[str] = []
    for key in ("maingroup", "catalog_level_2", "catalog_level_3", "wgtext"):
        value = normalize_space(str(fallback_product.get(key, "")))
        if value and value not in fallback_parts:
            fallback_parts.append(value)
    return " > ".join(fallback_parts)


def _parse_price(product: dict[str, Any]) -> str:
    current_size = product.get("current_delivery_size") or {}
    for key in ("regular_price", "price"):
        value = current_size.get(key)
        if isinstance(value, (int, float)) and value:
            return normalize_number(float(value) / 100)

    for key in ("sv_rebateprice", "sv_standardprice"):
        value = product.get(key)
        if isinstance(value, (int, float)) and value:
            return normalize_number(float(value) / 100)

    extfields = product.get("extfields") or {}
    uinfo = extfields.get("uinfo") or {}
    if isinstance(uinfo, dict):
        for units in uinfo.values():
            if not isinstance(units, list):
                continue
            for unit_info in units:
                for key in ("specialprice", "baseprice"):
                    value = unit_info.get(key)
                    if isinstance(value, (int, float)) and value:
                        return normalize_number(value)
    return ""


def _extract_gtins(product: dict[str, Any], sku: str) -> tuple[str, str]:
    gtin = ""
    bundle_gtin = ""
    for entry in product.get("barcodes", []) or []:
        if not isinstance(entry, dict):
            continue
        code = re.sub(r"\D+", "", str(entry.get("barcode", "")))
        if len(code) < 8 or code == sku:
            continue
        if not gtin:
            gtin = code
        if entry.get("quantity_as") not in (None, "", 0):
            bundle_gtin = code
            break
    if not bundle_gtin:
        bundle_gtin = gtin
    return gtin, bundle_gtin


def _extract_image_url(soup: BeautifulSoup) -> str:
    ld_json = soup.select_one('script[type="application/ld+json"]')
    if ld_json:
        try:
            payload = json.loads(ld_json.get_text())
        except json.JSONDecodeError:
            payload = {}
        image = payload.get("image")
        if isinstance(image, str):
            return image
    image = soup.select_one("img.detailpicture")
    if image and image.get("src"):
        return normalize_space(image["src"])
    return ""


def _strip_html_text(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(unescape(str(value)), "lxml").get_text(" ", strip=True)
    return normalize_space(text)


def _extract_country(product: dict[str, Any]) -> str:
    origin_all = product.get("origin_all") or {}
    origin_key = str(product.get("origin_key", "")).strip()
    if origin_key and isinstance(origin_all, dict):
        entry = origin_all.get(origin_key)
        if isinstance(entry, dict):
            value = normalize_space(str(entry.get("value", "")))
            if value:
                return value
    return _strip_html_text(product.get("origin"))


def _extract_labels(product: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for entry in product.get("labels", []) or []:
        if isinstance(entry, dict):
            for key in ("desc", "name", "value"):
                value = normalize_label(str(entry.get(key, "")))
                if value:
                    labels.append(value)
                    break
        else:
            value = normalize_label(str(entry))
            if value:
                labels.append(value)

    diet = normalize_space(str(product.get("diet", ""))).lower()
    if diet:
        labels.extend([normalize_label(part) for part in diet.split(",") if normalize_label(part)])

    unique_labels: list[str] = []
    for label in labels:
        if label and label not in unique_labels:
            unique_labels.append(label)
    return unique_labels


def _extract_product_sheet_url(base_url: str, product: dict[str, Any]) -> str:
    product_data_sheet = product.get("product_data_sheet")
    if isinstance(product_data_sheet, str) and product_data_sheet.strip():
        return absolute_url(base_url, product_data_sheet)

    for entry in product.get("product_data_sheets", []) or []:
        if isinstance(entry, str) and entry.strip():
            return absolute_url(base_url, entry)
        if isinstance(entry, dict):
            href = entry.get("url") or entry.get("href") or entry.get("path")
            if href:
                return absolute_url(base_url, str(href))
    return ""


def _parse_delivery_size_text(text: str) -> dict[str, str]:
    raw = normalize_space(text)
    parsed = {
        "vessel_type": "",
        "vessel_size": "",
        "vessel_unit": "",
        "bundle_size": "",
        "bundle_type": "",
        "net_weight": "",
        "total_weight": "",
    }
    if not raw:
        return parsed

    match = DELIVERY_SIZE_RE.match(raw)
    if not match:
        parsed["vessel_type"] = raw
        return parsed

    vessel_type = normalize_space(match.group("container"))
    value = normalize_number(match.group("value"))
    unit = normalize_unit(match.group("unit"))
    extra = normalize_space(match.group("extra") or "")

    parsed["vessel_type"] = vessel_type
    if unit in WEIGHT_UNITS | VOLUME_UNITS:
        parsed["vessel_size"] = value
        parsed["vessel_unit"] = unit
        if unit in WEIGHT_UNITS:
            parsed["net_weight"] = value
    else:
        if value != "1":
            parsed["bundle_size"] = value
            parsed["bundle_type"] = unit

    if extra:
        extra_match = WEIGHT_OR_VOLUME_RE.search(extra)
        if extra_match:
            extra_value = normalize_number(extra_match.group("value"))
            extra_unit = normalize_unit(extra_match.group("unit"))
            if not parsed["vessel_size"] and extra_unit in WEIGHT_UNITS | VOLUME_UNITS:
                parsed["vessel_size"] = extra_value
                parsed["vessel_unit"] = extra_unit
            if extra_unit in WEIGHT_UNITS:
                parsed["total_weight"] = extra_value
    return parsed


def _build_specs(product: dict[str, Any]) -> dict[str, str]:
    mapping = {
        "article_type": "article_type",
        "delivery_info": "info",
        "delivered_conservation_method": "delivered_conservation_method",
        "diet": "diet",
        "ingredients": "ingredient_list",
        "allergens": "allergens",
        "traces": "traces",
        "intolerance": "intolerance",
        "nutrition_energy": "nutrition_info_energy",
        "nutrition_fat": "nutritional_value_fat",
        "nutrition_saturated_fat": "nutritional_value_saturated_fatty_acids",
        "nutrition_carbohydrates": "nutritional_value_carbohydrates",
        "nutrition_sugar": "nutritional_value_sugar",
        "nutrition_fiber": "nutritional_value_fiber",
        "nutrition_protein": "nutritional_value_protein",
        "nutrition_salt": "nutritional_value_salt",
        "nutrition_amount": "nutritional_value_amount",
        "nutrition_unit": "nutritional_value_unit",
        "delivery_size": "currentdeliverysizetext",
        "vendor_name": "vendorname",
        "catalog_level_3": "catalog_level_3",
    }
    specs: dict[str, str] = {}
    for output_key, input_key in mapping.items():
        value = product.get(input_key)
        cleaned = _strip_html_text(value)
        if cleaned:
            specs[output_key] = cleaned
    minimum_delivery_date = normalize_space(str(product.get("minimum_delivery_date", "")))
    if minimum_delivery_date:
        specs["minimum_delivery_date"] = minimum_delivery_date
    return specs


def parse_product_record(
    html: str,
    url: str,
    *,
    product_payload: dict[str, Any] | None = None,
) -> NormalizedProduct | None:
    soup = BeautifulSoup(html, "lxml")
    product = product_payload or extract_product_payload(html)
    if not product:
        return None

    canonical_url = canonicalize_url(url)
    item_name = normalize_space(str(product.get("shorttext", "")))
    sku = normalize_space(str(product.get("extartnr", "")))
    gtin, bundle_gtin = _extract_gtins(product, sku)
    category_path = _extract_breadcrumb_path(soup, product)
    delivery_size_text = normalize_space(str(product.get("currentdeliverysizetext", "")))
    packaging = _parse_delivery_size_text(delivery_size_text)
    vat = normalize_number(product.get("mwst"))
    description = _strip_html_text(
        product.get("description")
        or product.get("detailtext")
        or product.get("text_generic")
        or product.get("sv_text_long")
    )
    manufacturer = normalize_space(str(product.get("manufacturer", "")))
    brand = normalize_space(str(product.get("brand", "")))
    product_sheet_url = _extract_product_sheet_url(canonical_url, product)
    price = _parse_price(product)
    min_order_count = normalize_number(
        ((product.get("extfields") or {}).get("gp_minord")) or 1
    ) or "1"
    currency = normalize_space(str(product.get("buying_currency", ""))) or "CHF"
    status = "ACTIVE" if int(product.get("status", 0) or 0) > 0 else "INACTIVE"
    image_url = _extract_image_url(soup)

    return NormalizedProduct(
        product_url=canonical_url,
        canonical_url=canonical_url,
        category_path=category_path,
        product_name=item_name,
        item_name=item_name,
        sku=sku,
        gtin=gtin,
        bundle_gtin=bundle_gtin,
        price=price,
        currency=currency,
        vat=vat,
        price_per="vessel",
        order_by="vessel",
        min_order_count=min_order_count,
        status=status,
        image_url=image_url,
        product_sheet_url=product_sheet_url,
        description=description,
        manufacturer=manufacturer,
        brand=brand,
        country=_extract_country(product),
        labels=_extract_labels(product),
        vessel_size=packaging["vessel_size"],
        vessel_unit=packaging["vessel_unit"],
        vessel_type=packaging["vessel_type"],
        bundle_size=packaging["bundle_size"],
        bundle_type=packaging["bundle_type"],
        raw_bundle_text=delivery_size_text,
        raw_fill_text=delivery_size_text,
        net_weight=packaging["net_weight"],
        total_weight=packaging["total_weight"],
        raw_availability_text="auf Anfr." if "auf Anfr." in html else "",
        specs=_build_specs(product),
    )


__all__ = [
    "extract_catalog_categories",
    "extract_pagination_links",
    "extract_product_links",
    "extract_product_payload",
    "parse_product_record",
    "product_candidate_from_url",
]
