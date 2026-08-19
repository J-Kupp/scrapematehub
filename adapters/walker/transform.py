from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from models import NormalizedProduct


MULTIPACK_RE = re.compile(
    r"\b(?P<count>\d[\d'.,]*)\s*x\s*(?P<size>\d[\d'.,]*)\s*"
    r"(?P<unit>ml|cl|dl|lt|l|kg|g)\b",
    re.IGNORECASE,
)
PIECE_COUNT_RE = re.compile(
    r"\b(?P<count>\d[\d'.,]*)\s*(?:stück|stueck|stk\.?)\b",
    re.IGNORECASE,
)
SINGLE_SIZE_RE = re.compile(
    r"\b(?P<size>\d[\d'.,]*)\s*(?P<unit>ml|cl|dl|lt|l|kg|g)\b",
    re.IGNORECASE,
)
PRICE_RE = re.compile(r"(?:CHF\s*)?(?P<price>\d[\d' ]*(?:[.,]\d{1,2})?)")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def normalize_number(value: str) -> str:
    cleaned = value.replace("'", "").replace("’", "").replace(" ", "").replace(",", ".")
    return cleaned[:-2] if cleaned.endswith(".0") else cleaned


def normalize_unit(value: str) -> str:
    return "l" if value.lower() == "lt" else value.lower()


def product_candidate_from_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc.lower() != "shop.walker.swiss":
        return False
    path = parsed.path
    return bool(
        path.lower().startswith("/de/alle-produkte/")
        and re.search(r"-\d+(?:-\d+)?\.html$", path, re.IGNORECASE)
    )


def extract_sitemap_urls(xml_text: str) -> list[str]:
    return re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text, flags=re.IGNORECASE)


def extract_german_product_urls(xml_text: str) -> set[str]:
    return {url for url in extract_sitemap_urls(xml_text) if product_candidate_from_url(url)}


def parse_definition_list(soup: BeautifulSoup) -> tuple[dict[str, str], list[str]]:
    specs: dict[str, str] = {}
    labels: list[str] = []
    for term in soup.select("dl.article-spec-infos dt"):
        definition = term.find_next_sibling("dd")
        if definition is None:
            continue
        key = normalize_space(term.get_text(" ", strip=True)).rstrip(":")
        values = [normalize_space(value) for value in definition.stripped_strings]
        values = [value for value in values if value]
        if not key or not values:
            continue
        specs[key] = " | ".join(values)
        if key.lower() == "label":
            labels.extend(values)
    return specs, sorted(set(labels))


def parse_category_path(soup: BeautifulSoup) -> str:
    categories: list[str] = []
    for anchor in soup.select(".opc-breadcrumb .breadcrumb-navigation a"):
        value = normalize_space(anchor.get_text(" ", strip=True))
        if value.lower() in {"home", "alle produkte"}:
            continue
        if value:
            categories.append(value)
    return " > ".join(categories) if categories else "Uncategorized"


def parse_gtin(soup: BeautifulSoup) -> str:
    for anchor in soup.select("dl.article-spec-infos a[href]"):
        values = parse_qs(urlparse(anchor.get("href", "")).query).get("gtin", [])
        if values:
            digits = re.sub(r"\D", "", values[0])
            if 8 <= len(digits) <= 14:
                return digits
    return ""


def parse_packaging(name: str) -> tuple[str, str, str, str, str]:
    multipack = MULTIPACK_RE.search(name)
    if multipack:
        count = normalize_number(multipack.group("count"))
        return (
            normalize_number(multipack.group("size")),
            normalize_unit(multipack.group("unit")),
            count if count != "1" else "",
            "Pack" if count != "1" else "",
            normalize_space(multipack.group(0)),
        )

    piece_count = PIECE_COUNT_RE.search(name)
    if piece_count:
        count = normalize_number(piece_count.group("count"))
        return "1", "quantity", count if count != "1" else "", "Pack" if count != "1" else "", normalize_space(piece_count.group(0))

    matches = list(SINGLE_SIZE_RE.finditer(name))
    if matches:
        size = matches[-1]
        return normalize_number(size.group("size")), normalize_unit(size.group("unit")), "", "", normalize_space(size.group(0))
    return "", "", "", "", ""


def parse_price(soup: BeautifulSoup) -> str:
    node = soup.select_one(".article-list-item-price")
    if node is None:
        return ""
    match = PRICE_RE.search(normalize_space(node.get_text(" ", strip=True)))
    return normalize_number(match.group("price")) if match else ""


def parse_product_record(html: str, url: str) -> NormalizedProduct | None:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("main .page-details, main .article-head")
    heading = soup.select_one("main .article-infos h1, main h1")
    if root is None or heading is None:
        return None

    name = normalize_space(heading.get_text(" ", strip=True))
    if not name:
        return None

    canonical = soup.select_one("link[rel='canonical']")
    canonical_url = canonical.get("href", url) if canonical else url
    specs, labels = parse_definition_list(soup)
    sku = specs.get("Artikelnummer", "")
    gtin = parse_gtin(soup)
    image = soup.select_one("main .article-image img[src]")
    image_url = urljoin(url, image.get("src", "")) if image else ""
    vessel_size, vessel_unit, bundle_size, bundle_type, packaging_text = parse_packaging(name)
    country = "CH" if any("schweizer produkt" in label.lower() for label in labels) else ""

    return NormalizedProduct(
        product_url=url,
        canonical_url=canonical_url,
        category_path=parse_category_path(soup),
        product_name=name,
        item_name=name,
        sku=sku,
        gtin=gtin,
        price=parse_price(soup),
        currency="CHF",
        status="ACTIVE",
        image_url=image_url,
        country=country,
        labels=labels,
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        bundle_size=bundle_size,
        bundle_type=bundle_type,
        raw_bundle_text=packaging_text,
        raw_spec_piece_text=packaging_text if vessel_unit == "quantity" else "",
        raw_fill_text=packaging_text if vessel_unit not in {"", "quantity"} else "",
        specs=specs,
    )


__all__ = [
    "extract_german_product_urls",
    "extract_sitemap_urls",
    "parse_product_record",
    "product_candidate_from_url",
]
