from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from models import NormalizedProduct


PRODUCT_URL_RE = re.compile(r"^https?://[^/]+/.+-\d+\.html$", re.IGNORECASE)
PRODUCT_IDENTIFIER_RE = re.compile(r"-(?P<product_id>\d+)\.html$", re.IGNORECASE)
COUNT_UNITS = {"stk", "st", "stück", "stueck"}
WEIGHT_UNITS = {"g", "kg"}
VOLUME_UNITS = {"ml", "cl", "dl", "l"}
PACKAGING_RE = re.compile(r"(?P<value>\d[\d.,]*)\s*(?P<unit>kg|g|ml|cl|dl|l|stk|st)\b", re.IGNORECASE)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def normalize_number(value: Any) -> str:
    text = normalize_space(str(value or "")).replace("'", "").replace("’", "").replace(",", ".")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    return f"{number:.4f}".rstrip("0").rstrip(".")


def normalize_unit(value: str) -> str:
    unit = normalize_space(value).lower()
    if unit in {"stück", "stueck", "stk"}:
        return "st"
    return unit


def absolute_url(base_url: str, href: str | None) -> str:
    if not href:
        return ""
    return urljoin(base_url, href)


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def product_candidate_from_url(url: str) -> bool:
    return bool(PRODUCT_URL_RE.match(canonicalize_url(url.strip())))


def product_identifier_from_url(url: str) -> str:
    """Return Walker's stable article ID even when a product has several category URLs."""
    match = PRODUCT_IDENTIFIER_RE.search(canonicalize_url(url.strip()))
    return match.group("product_id") if match else ""


def extract_listing_product_total(html: str) -> int:
    """Read Walker's advertised result count from a product listing page."""
    soup = BeautifulSoup(html, "lxml")
    progress = soup.select_one(".progressbar-wrapper[data-amount]")
    if not progress:
        return 0
    value = re.sub(r"\D", "", str(progress.get("data-amount", "")))
    return int(value) if value else 0


def extract_product_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = {
        absolute_url(base_url, anchor.get("href"))
        for anchor in soup.select("article.article-list-item a[href]")
        if product_candidate_from_url(absolute_url(base_url, anchor.get("href")))
    }
    return sorted(urls)


def extract_category_links(html: str, base_url: str) -> list[str]:
    """Return Walker category and subcategory listing pages, never product detail URLs."""
    soup = BeautifulSoup(html, "lxml")
    listing_prefix = "/de/alle-produkte/"
    urls: set[str] = set()
    for anchor in soup.select("a[href]"):
        url = canonicalize_url(absolute_url(base_url, anchor.get("href")))
        parsed = urlparse(url)
        path = parsed.path.lower()
        if (
            path.startswith(listing_prefix)
            and path.endswith("/")
            and not parsed.query
            and not product_candidate_from_url(url)
        ):
            urls.add(url)
    return sorted(urls)


def extract_next_listing_url(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("[data-op-href]"):
        href = absolute_url(base_url, node.get("data-op-href"))
        if "pposCatItem=" in href:
            return href
    return ""


def extract_tab_targets(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    targets: list[str] = []
    for node in soup.select("#tabsPart1[data-op-target], #tabsPart2[data-op-target]"):
        target = absolute_url(base_url, node.get("data-op-target"))
        if target and target not in targets:
            targets.append(target)
    return targets


def extract_manufacturer_link(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for dt in soup.select("dl.article-spec-infos dt"):
        key = normalize_space(dt.get_text(" ", strip=True)).lower()
        dd = dt.find_next_sibling("dd")
        if key == "link" and dd:
            link = dd.select_one("a[href]")
            return absolute_url(base_url, link.get("href")) if link else ""
    return ""


def extract_spec_map(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    specs: dict[str, str] = {}
    for dt in soup.select("dl.article-spec-infos dt"):
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        key = normalize_space(dt.get_text(" ", strip=True)).rstrip(":")
        if key.lower() == "label":
            value = "\n".join(normalize_space(part) for part in dd.stripped_strings if normalize_space(part))
        else:
            value = normalize_space(dd.get_text(" ", strip=True))
        if key and value:
            specs[key] = value
    return specs


def _extract_breadcrumb_path(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    for item in soup.select(".opc-breadcrumb .breadcrumb-navigation li"):
        value = normalize_space(item.get_text(" ", strip=True))
        if value and value != "Home":
            parts.append(value)
    return " > ".join(parts[:-1]) if len(parts) > 1 else ""


def _extract_labels(specs: dict[str, str]) -> list[str]:
    labels_raw = specs.get("Label", "")
    if not labels_raw:
        return []
    labels = [normalize_space(part) for part in re.split(r"[\n,]+", labels_raw) if normalize_space(part)]
    return labels


def _extract_image_url(soup: BeautifulSoup, base_url: str) -> str:
    image = soup.select_one(".article-image img[src]")
    if image:
        return absolute_url(base_url, image.get("src"))
    return ""


def _extract_name(soup: BeautifulSoup) -> str:
    heading = soup.select_one("h1")
    return normalize_space(heading.get_text(" ", strip=True)) if heading else ""


def _parse_packaging(title: str) -> tuple[str, str, str, str]:
    vessel_size = ""
    vessel_unit = ""
    bundle_size = ""
    bundle_type = ""
    matches = list(PACKAGING_RE.finditer(title))
    if matches:
        match = matches[-1]
        vessel_size = normalize_number(match.group("value"))
        vessel_unit = normalize_unit(match.group("unit"))
        return vessel_size, vessel_unit, bundle_size, bundle_type

    tail = normalize_space(title).split(" ")[-1].lower() if title else ""
    if tail in COUNT_UNITS:
        return "1", "st", "", ""
    if tail in WEIGHT_UNITS | VOLUME_UNITS:
        return "1", normalize_unit(tail), "", ""
    return "", "", "", ""


def _parse_external_page(external_html: str | None, external_url: str | None) -> dict[str, str]:
    if not external_html or not external_url:
        return {}
    soup = BeautifulSoup(external_html, "lxml")
    title = normalize_space(soup.title.get_text(" ", strip=True)) if soup.title else ""
    meta = soup.select_one('meta[name="description"], meta[property="og:description"]')
    meta_description = normalize_space(meta.get("content", "")) if meta else ""
    image = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
    image_url = absolute_url(external_url, image.get("content")) if image and image.get("content") else ""
    pdf_links = [
        absolute_url(external_url, link.get("href"))
        for link in soup.select('a[href$=".pdf"]')
        if link.get("href")
    ]
    paragraphs: list[str] = []
    for node in soup.select("main p, article p, .content p"):
        text = normalize_space(node.get_text(" ", strip=True))
        if text and text not in paragraphs:
            paragraphs.append(text)
        if len(" ".join(paragraphs)) >= 500:
            break
    return {
        "external_title": title,
        "external_description": meta_description or " ".join(paragraphs[:3]),
        "external_image_url": image_url,
        "external_pdf_url": pdf_links[0] if pdf_links else "",
    }


def parse_product_record(
    html: str,
    product_url: str,
    *,
    external_html: str | None = None,
    external_url: str | None = None,
) -> NormalizedProduct | None:
    soup = BeautifulSoup(html, "lxml")
    title = _extract_name(soup)
    if not title:
        return None

    specs = extract_spec_map(html)
    labels = _extract_labels(specs)
    image_url = _extract_image_url(soup, product_url)
    category_path = _extract_breadcrumb_path(soup)
    vessel_size, vessel_unit, bundle_size, bundle_type = _parse_packaging(title)
    external = _parse_external_page(external_html, external_url)

    description_parts: list[str] = []
    legal_nodes = soup.select(".accordion-body p")
    if legal_nodes:
        legal_note = " ".join(normalize_space(node.get_text(" ", strip=True)) for node in legal_nodes)
        if legal_note:
            description_parts.append(legal_note)
    external_description = external.get("external_description", "")
    if external_description:
        description_parts.append(external_description)

    normalized_specs: dict[str, str] = {}
    for key, value in specs.items():
        normalized_specs[key.lower().replace(" ", "_")] = value
    if external_url and not external_url.lower().endswith(".pdf"):
        normalized_specs["manufacturer_link"] = external_url
    if external.get("external_title"):
        normalized_specs["manufacturer_page_title"] = external["external_title"]
    if external.get("external_image_url"):
        normalized_specs["manufacturer_image_url"] = external["external_image_url"]

    product = NormalizedProduct(
        product_url=product_url,
        canonical_url=canonicalize_url(product_url),
        category_path=category_path,
        product_name=title,
        item_name=title,
        sku=specs.get("Artikelnummer", ""),
        price="",
        currency="CHF",
        vat="",
        image_url=image_url or external.get("external_image_url", ""),
        product_sheet_url=external_url if (external_url or "").lower().endswith(".pdf") else external.get("external_pdf_url", ""),
        description="\n\n".join(part for part in description_parts if part),
        manufacturer=external.get("external_title", ""),
        brand="Walker",
        region=specs.get("Herkunft", ""),
        country=specs.get("Herkunft", ""),
        labels=labels,
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        vessel_type="piece" if vessel_unit == "st" else "",
        bundle_size=bundle_size,
        bundle_type=bundle_type,
        specs=normalized_specs,
    )
    return product
