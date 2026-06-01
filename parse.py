from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from models import CSV_COLUMNS, NormalizedProduct


ROOT_CONTENT_SLUGS = {
    "agb",
    "buero",
    "klimaschutz",
    "datenschutz",
    "deko-und-ladenzubehoer",
    "displaymaterial",
    "essen",
    "festivals",
    "gastrobedarf",
    "hygiene-und-bedarfsartikel",
    "impressum",
    "mehrwegartikel-mieten",
    "mehrweggeschirr-mieten",
    "oeko-line-label",
    "oeko-line-produkte",
    "reinigung",
    "sale",
    "services",
    "trinken",
    "unternehmen",
}


VOLUME_RE = re.compile(
    r"(?P<value>\d[\d'.,]*)\s*(?P<unit>ml|cl|dl|l|liter|litre|kg|kilo|g|gramm)\b",
    re.IGNORECASE,
)
DIAMETER_RE = re.compile(r"(?:ø|Ø|diameter)\s*(?P<value>\d[\d'.,]*)\s*mm", re.IGNORECASE)
DIMENSIONS_RE = re.compile(
    r"(?P<l>\d[\d'.,]*)\s*x\s*(?P<w>\d[\d'.,]*)\s*x\s*(?P<h>\d[\d'.,]*)\s*mm",
    re.IGNORECASE,
)
TWO_DIMENSIONS_RE = re.compile(
    r"(?P<l>\d[\d'.,]*)\s*x\s*(?P<w>\d[\d'.,]*)\s*mm",
    re.IGNORECASE,
)
BUNDLE_RE = re.compile(
    r"(?P<size>\d[\d'.,]*)\s*(?:x\s*\d[\d'.,]*\s*(?:ml|cl|dl|l|kg|g)\s*)?(?:[A-Za-zÄÖÜäöüß.-]+\s*)?(?:/\s*(?P<bundle>[A-Za-zÄÖÜäöüß]+))",
    re.IGNORECASE,
)
VAT_RE = re.compile(r"(\d{1,2}(?:[.,]\d+)?)\s*%")
GTIN_RE = re.compile(r"\b(?:gtin|ean)\b[:\s]*([0-9]{8,14})", re.IGNORECASE)


RawProductRecord = NormalizedProduct


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def normalize_number(value: str) -> str:
    cleaned = value.replace("'", "").replace("’", "").replace(" ", "").replace(",", ".")
    if cleaned.endswith(".0"):
        cleaned = cleaned[:-2]
    return cleaned


def normalize_unit(value: str) -> str:
    lowered = value.lower()
    if lowered in {"liter", "litre", "l"}:
        return "l"
    if lowered in {"kilo", "kg"}:
        return "kg"
    if lowered in {"gramm", "g"}:
        return "g"
    return lowered


def text_or_empty(node: Any) -> str:
    if node is None:
        return ""
    return normalize_space(node.get_text(" ", strip=True))


def first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        value = text_or_empty(node)
        if value:
            return value
    return ""


def absolute_url(base_url: str, href: str | None) -> str:
    if not href:
        return ""
    return urljoin(base_url, href)


def parse_specs(description_root: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for row in description_root.select("table tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        key = normalize_space(cells[0].get_text(" ", strip=True)).rstrip(":")
        value = normalize_space(cells[1].get_text(" ", strip=True))
        if key and value:
            specs[key] = value
    return specs


def parse_selected_variant_options(description_root: BeautifulSoup) -> dict[str, str]:
    options: dict[str, str] = {}
    for group in description_root.select(".product-detail-configurator-group"):
        legend = text_or_empty(group.select_one("legend"))
        legend = legend.replace(" auswählen", "").replace("\u00a0auswählen", "")
        checked = group.select_one("input[type='radio'][checked]")
        if not checked or not checked.get("id"):
            continue
        label = group.select_one(f"label[for='{checked['id']}']")
        value = text_or_empty(label)
        if legend and value:
            options[legend] = value
    return options


def parse_bundle_info(bundle_text: str) -> tuple[str, str]:
    if not bundle_text:
        return "", ""
    if re.search(r"^\s*\d[\d'.,]*\s*(?:ml|cl|dl|l|liter|litre|kg|kilo|g|gramm)\s*/\s*[A-Za-zÄÖÜäöüß]+", bundle_text, re.IGNORECASE):
        return "", ""
    match = BUNDLE_RE.search(bundle_text)
    if not match:
        return "", ""
    size = normalize_number(match.group("size"))
    bundle_type = normalize_space(match.group("bundle"))
    if size == "1":
        return "", ""
    return size, bundle_type


def parse_dimensions(name: str, specs: dict[str, str]) -> tuple[str, str, str, str]:
    haystacks = [specs.get("Grösse", ""), specs.get("Größe", ""), name]
    for haystack in haystacks:
        match = DIMENSIONS_RE.search(haystack)
        if match:
            return (
                normalize_number(match.group("l")),
                normalize_number(match.group("w")),
                normalize_number(match.group("h")),
                "",
            )
    for haystack in haystacks:
        match = TWO_DIMENSIONS_RE.search(haystack)
        if match:
            return (
                normalize_number(match.group("l")),
                normalize_number(match.group("w")),
                "",
                "",
            )
    for haystack in haystacks:
        match = DIAMETER_RE.search(haystack)
        if match:
            return "", "", "", normalize_number(match.group("value"))
    return "", "", "", ""


def parse_vessel(name: str, specs: dict[str, str], bundle_text: str, fill_text: str) -> tuple[str, str]:
    haystacks = [fill_text, name, specs.get("Grösse", ""), specs.get("Inhalt", ""), bundle_text]
    for haystack in haystacks:
        match = VOLUME_RE.search(haystack)
        if match:
            return normalize_number(match.group("value")), normalize_unit(match.group("unit"))
    if bundle_text:
        return "1", "quantity"
    return "", ""


def infer_vessel_type(name: str) -> str:
    lowered = name.lower()
    mapping = [
        ("becher", "cup"),
        ("kaffeebecher", "cup"),
        ("suppenbecher", "cup"),
        ("schale", "bowl"),
        ("bowl", "bowl"),
        ("teller", "plate"),
        ("platte", "plate"),
        ("flasche", "bottle"),
        ("kanister", "container"),
        ("deckel", "lid"),
        ("box", "box"),
        ("menübox", "box"),
        ("menuebox", "box"),
        ("container", "container"),
        ("serviette", "napkin"),
        ("trinkhalm", "straw"),
        ("besteck", "cutlery"),
        ("beutel", "bag"),
        ("tragetasche", "bag"),
    ]
    for needle, vessel_type in mapping:
        if needle in lowered:
            return vessel_type
    return ""


def parse_color(name: str, specs: dict[str, str]) -> str:
    color = specs.get("Farbe", "")
    if color:
        return color.lower()
    palette = [
        "braun",
        "schwarz",
        "transparent",
        "weiss",
        "weiß",
        "rot",
        "blau",
        "grün",
        "gruen",
        "gelb",
        "pink",
        "orange",
        "gold",
        "silber",
    ]
    lowered = name.lower()
    for candidate in palette:
        if candidate in lowered:
            return candidate.replace("weiß", "weiss").replace("gruen", "grün")
    return ""


def parse_material(name: str, specs: dict[str, str]) -> str:
    material = specs.get("Material", "")
    if material:
        return material
    candidates = [
        "Palmblatt",
        "PLA",
        "PET",
        "Kraftpapier",
        "Holz",
        "Aluminium",
        "Edelstahl",
        "Papier",
        "Karton",
        "Bagasse",
        "Zuckerrohr",
        "Bambus",
    ]
    for candidate in candidates:
        if candidate.lower() in name.lower():
            return candidate
    return ""


def parse_labels(name: str, description: str, page_text: str, soup: BeautifulSoup) -> list[str]:
    labels: list[str] = []
    lowered = f"{name} {description} {page_text}".lower()
    if any(token in lowered for token in ["öko-line", "oeko-line", "bio", "biologisch abbaubar", "kompostierbar", "nachhaltig"]):
        labels.append("BIO")
    badge_text = " ".join(text_or_empty(node) for node in soup.select(".product-badges, .badge"))
    badge_lower = badge_text.lower()
    if "neu" in badge_lower or "new" in badge_lower:
        labels.append("NEW")
    if "sale" in badge_lower or "aktion" in badge_lower or "statt" in lowered:
        labels.append("DISCOUNTED")
    if "saison" in badge_lower or "season" in badge_lower:
        labels.append("SEASONAL")
    return sorted(set(labels))


def parse_gtin(page_text: str, specs: dict[str, str]) -> str:
    for key, value in specs.items():
        if key.lower() in {"ean", "gtin"}:
            digits = re.sub(r"\D", "", value)
            if 8 <= len(digits) <= 14:
                return digits
    match = GTIN_RE.search(page_text)
    if match:
        return match.group(1)
    return ""


def parse_vat(text: str) -> str:
    match = VAT_RE.search(text)
    if not match:
        return ""
    return normalize_number(match.group(1))


def parse_status(availability_text: str, soup: BeautifulSoup) -> str:
    lowered = availability_text.lower()
    if any(token in lowered for token in ["nicht verfügbar", "ausverkauft", "out of stock", "momentan nicht verfügbar"]):
        return "OUT_OF_STOCK"
    if any(token in lowered for token in ["eingestellt", "discontinued", "nicht mehr lieferbar"]):
        return "INACTIVE"
    if soup.select_one("[itemprop='availability'][href*='InStock']") or "verfügbar" in lowered or "lieferzeit" in lowered:
        return "ACTIVE"
    return "ACTIVE"


def clean_description(description_root: BeautifulSoup) -> str:
    clone = BeautifulSoup(str(description_root), "lxml")
    for selector in ["h1", "table", "img", ".mt-4", "script", "style"]:
        for node in clone.select(selector):
            node.decompose()
    text = normalize_space(" ".join(clone.stripped_strings))
    return text


def parse_category_path(soup: BeautifulSoup) -> str:
    breadcrumbs = []
    for node in soup.select(".breadcrumb .breadcrumb-title"):
        value = text_or_empty(node)
        if value and value.lower() != "home":
            breadcrumbs.append(value)
    return " > ".join(breadcrumbs) if breadcrumbs else "Uncategorized"


def build_item_name(name: str, variant_options: dict[str, str]) -> str:
    option_values = [value for value in variant_options.values() if value]
    if not option_values:
        return name
    normalized_name = name.lower().replace("kl.m.", "kleinmenge").replace("std.", "standardmenge")
    missing = [value for value in option_values if value.lower() not in normalized_name]
    if not missing:
        return name
    return f"{name} - {' / '.join(missing)}"


def product_candidate_from_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or url).strip("/")
    if not path:
        return False
    segments = path.split("/")
    if len(segments) == 2 and segments[0] == "detail" and segments[1]:
        return True
    if len(segments) != 1:
        return False
    return segments[0] not in ROOT_CONTENT_SLUGS


def parse_product_page(html: str, url: str) -> RawProductRecord | None:
    soup = BeautifulSoup(html, "lxml")
    detail_root = soup.select_one(".product-detail-content, .cms-page[itemtype='https://schema.org/Product']")
    if detail_root is None:
        return None

    name = first_text(soup, [".product-detail-name", "h1[itemprop='name']", "meta[property='og:title']"])
    if not name:
        return None

    canonical_node = soup.select_one("link[rel='canonical']")
    canonical_url = canonical_node.get("href", url) if canonical_node else url
    description_root = soup.select_one(".product-detail-description-text") or detail_root
    specs = parse_specs(description_root)
    detail_price_unit = first_text(soup, [".product-detail-price-unit .price-unit-content"])
    variant_options = parse_selected_variant_options(soup)
    description = clean_description(description_root)
    page_text = normalize_space(detail_root.get_text(" ", strip=True))
    category_path = parse_category_path(soup)
    sku = first_text(
        soup,
        [
            ".product-detail-ordernumber[itemprop='sku']",
            ".product-detail-ordernumber",
        ],
    )
    price = first_text(soup, ["meta[itemprop='price']", ".product-detail-price"])
    price = price.replace("CHF", "").replace("*", "").strip()
    price = normalize_number(price) if price else ""
    currency = first_text(soup, ["meta[itemprop='priceCurrency']", "meta[property='product:price:currency']"])
    image_url = absolute_url(url, soup.select_one("meta[property='og:image']").get("content") if soup.select_one("meta[property='og:image']") else "")
    product_sheet = ""
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if ".pdf" in href.lower():
            product_sheet = absolute_url(url, href)
            break

    availability_text = first_text(soup, [".delivery-information", ".product-delivery-information"])
    spec_piece_text = specs.get("Stückzahl", "") or specs.get("Anzahl", "")
    fill_text = specs.get("Füllmenge", "") or specs.get("Inhalt", "")
    bundle_text = detail_price_unit or spec_piece_text
    vessel_size, vessel_unit = parse_vessel(name, specs, bundle_text, fill_text)
    bundle_size, bundle_type = parse_bundle_info(bundle_text or spec_piece_text)
    length, width, height, diameter = parse_dimensions(name, specs)
    labels = parse_labels(name, description, page_text, soup)
    item_name = build_item_name(name, variant_options)
    variant_name = " / ".join(value for value in variant_options.values() if value)
    vat = parse_vat(page_text)

    if vessel_unit == "quantity" and not vessel_size:
        vessel_size = "1"

    material = parse_material(name, specs)
    color = parse_color(name, specs)
    gtin = parse_gtin(page_text, specs)

    return RawProductRecord(
        product_url=url,
        canonical_url=canonical_url,
        category_path=category_path,
        product_name=name,
        item_name=item_name,
        sku=sku,
        variant_name=variant_name,
        variant_options=variant_options,
        gtin=gtin,
        bundle_gtin="",
        price=price,
        currency=currency or "CHF",
        vat=vat,
        price_per="vessel",
        order_by="vessel",
        min_order_count="1",
        status=parse_status(availability_text, soup),
        image_url=image_url,
        product_sheet_url=product_sheet,
        description=description,
        manufacturer=specs.get("Hersteller", ""),
        brand=specs.get("Marke", ""),
        region=specs.get("Region", ""),
        country=specs.get("Land", "") or specs.get("Herkunft", ""),
        labels=labels,
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        vessel_type=infer_vessel_type(name),
        bundle_size=bundle_size,
        bundle_type=bundle_type,
        raw_bundle_text=bundle_text,
        raw_detail_price_unit_text=detail_price_unit,
        raw_spec_piece_text=spec_piece_text,
        raw_fill_text=fill_text,
        color=color,
        material=material,
        length=length,
        width=width,
        height=height,
        diameter=diameter,
        net_weight="",
        total_weight="",
        raw_availability_text=availability_text,
        specs=specs,
    )
