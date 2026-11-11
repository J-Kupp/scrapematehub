from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from models import NormalizedProduct


PACKAGING_RE = re.compile(
    r"(?:(?P<bundle>\d+)\s*[x×]\s*)?(?:ca\.\s*)?(?P<size>\d[\d.,]*)\s*(?P<unit>kg|g|ml|cl|dl|l|stück|st\.)\b",
    re.IGNORECASE,
)
PRODUCT_ID_RE = re.compile(r"/catalog2/products/(?P<product_id>\d+)", re.IGNORECASE)


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def normalize_number(value: Any) -> str:
    text = normalize_space(value).replace("'", "").replace("’", "").replace(",", ".")
    if not text:
        return ""
    try:
        return f"{float(text):.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return text


def absolute_url(base_url: str, href: str | None) -> str:
    return urljoin(base_url, href or "")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def product_identifier_from_url(url: str) -> str:
    match = PRODUCT_ID_RE.search(urlparse(url).path)
    return match.group("product_id") if match else ""


def extract_root_category_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = {
        canonicalize_url(absolute_url(base_url, node.get("href")))
        for node in soup.select("li.maincategory > a[href]")
    }
    return sorted(url for url in urls if "/catalog2/" in url and "/products/" not in url)


def extract_product_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = {
        canonicalize_url(absolute_url(base_url, node.get("href")))
        for node in soup.select('a[href*="/catalog2/products/"]')
    }
    return sorted(url for url in urls if product_identifier_from_url(url))


def extract_next_listing_url(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("a.paginator-list__nav--next:not(.paginator-list__nav--disabled)[href]")
    return absolute_url(base_url, node.get("href")) if node else ""


def _product_payload(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select('script[data-store="react-components"]'):
        try:
            components = json.loads(script.get_text())
        except json.JSONDecodeError:
            continue
        for component in components.values():
            if not isinstance(component, dict):
                continue
            if component.get("constructor_type") != "cust/mundo/mundo-detail-intro$default":
                continue
            product = (component.get("props") or {}).get("product")
            if isinstance(product, dict):
                return product
    return {}


def _breadcrumb_path(soup: BeautifulSoup) -> str:
    parts = [normalize_space(node.get_text(" ", strip=True)) for node in soup.select(".breadcrumb__item")]
    parts = [part for part in parts if part and part not in {"Katalog", "Zurück"}]
    return " > ".join(parts[:-1]) if len(parts) > 1 else ""


def _image_url(soup: BeautifulSoup, product_url: str) -> str:
    image = soup.select_one('meta[property="og:image"], .product-image img[src]')
    return absolute_url(product_url, image.get("content") or image.get("src")) if image else ""


def _product_sheet_url(soup: BeautifulSoup, product_url: str) -> str:
    for link in soup.select('a[href]'):
        label = normalize_space(link.get_text(" ", strip=True)).lower()
        if "produktdatenblatt" in label or "datenblatt" in label:
            url = absolute_url(product_url, link.get("href"))
            if urlparse(url).scheme in {"http", "https"}:
                return url
    return ""


def _packaging(value: str) -> tuple[str, str, str, str]:
    match = PACKAGING_RE.search(value)
    if not match:
        return "", "", "", ""
    unit = match.group("unit").lower().rstrip(".")
    if unit in {"stück", "st"}:
        unit = "st"
    return (
        normalize_number(match.group("size")),
        unit,
        normalize_number(match.group("bundle")),
        "pack" if match.group("bundle") else "",
    )


def _dietary_labels(payload: dict[str, Any]) -> list[str]:
    labels = [normalize_space(label) for label in payload.get("labels") or []]
    for key, label in (
        ("diet_vegetarian", "vegetarisch"),
        ("diet_vegan", "vegan"),
        ("diet_gluten_free", "glutenfrei"),
        ("diet_lactose_free", "laktosefrei"),
    ):
        if payload.get(key) and label not in labels:
            labels.append(label)
    return labels


def _allergens(payload: dict[str, Any]) -> str:
    allergens = []
    for key, value in payload.items():
        if not key.startswith("contains_") or value not in {1, True}:
            continue
        allergen = key.removeprefix("contains_").replace("_", " ")
        allergens.append(allergen)
    return ", ".join(allergens)


def _nutrition(payload: dict[str, Any]) -> str:
    labels = {
        "nutritional_value_joules": "Energie",
        "nutritional_value_calories": "Kalorien",
        "nutritional_value_fat": "Fett",
        "nutritional_value_saturated_fatty_acids": "Gesättigte Fettsäuren",
        "nutritional_value_carbohydrates": "Kohlenhydrate",
        "nutritional_value_sugar": "Zucker",
        "nutritional_value_fiber": "Ballaststoffe",
        "nutritional_value_protein": "Eiweiss",
        "nutritional_value_salt": "Salz",
    }
    values = [f"{label}: {normalize_space(payload.get(key))}" for key, label in labels.items() if payload.get(key) not in (None, "")]
    amount = normalize_space(payload.get("nutritional_value_amount"))
    unit = normalize_space(payload.get("nutritional_value_unit"))
    prefix = f"pro {amount} {unit}".strip() if amount else ""
    return "; ".join([prefix, *values]) if values else ""


def parse_product_record(html: str, product_url: str) -> NormalizedProduct | None:
    soup = BeautifulSoup(html, "lxml")
    payload = _product_payload(soup)
    name = normalize_space(payload.get("shorttext"))
    sku = normalize_space(payload.get("extartnr")) or product_identifier_from_url(product_url)
    if not name or not sku:
        return None

    packing = normalize_space(payload.get("packing_text_override") or payload.get("packing") or payload.get("currentdeliverysizetext"))
    vessel_size, vessel_unit, bundle_size, bundle_type = _packaging(packing)
    description = normalize_space(payload.get("detailtext") or payload.get("description"))
    ingredients = normalize_space(payload.get("ingredient_list"))
    specs = {
        "mundo_product_id": normalize_space(payload.get("prodid")),
        "supplier": normalize_space(payload.get("supplier")),
        "storage_condition": normalize_space(payload.get("delivered_conservation_method")),
        "transport": normalize_space(payload.get("transport")),
        "season_start": normalize_space(payload.get("season_start")),
        "season_end": normalize_space(payload.get("season_end")),
        "packaging": packing,
    }
    specs = {key: value for key, value in specs.items() if value}
    return NormalizedProduct(
        product_url=product_url,
        canonical_url=canonicalize_url(product_url),
        category_path=_breadcrumb_path(soup) or " > ".join(
            part for part in (normalize_space(payload.get("maingroup")), normalize_space(payload.get("catalog_level_2")), normalize_space(payload.get("catalog_level_3"))) if part
        ),
        product_name=name,
        item_name=name,
        sku=sku,
        currency="CHF",
        image_url=_image_url(soup, product_url),
        product_sheet_url=_product_sheet_url(soup, product_url),
        description=description,
        manufacturer=normalize_space(payload.get("manufacturer") or payload.get("supplier")),
        brand=normalize_space(payload.get("brand") or payload.get("ext_make")),
        country=normalize_space(payload.get("manufacturing_country") or payload.get("origin")),
        labels=_dietary_labels(payload),
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        bundle_size=bundle_size,
        bundle_type=bundle_type,
        raw_bundle_text=packing,
        raw_detail_price_unit_text=packing,
        ingredients=ingredients,
        allergens=_allergens(payload),
        storage_advice=normalize_space(payload.get("delivered_conservation_method")),
        nutritional_values=_nutrition(payload),
        dietary_labels=", ".join(_dietary_labels(payload)),
        specs=specs,
    )
