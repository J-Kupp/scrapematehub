from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from models import NormalizedProduct


PACKAGING_RE = re.compile(r"(?P<size>\d[\d.,]*)\s*(?P<unit>cl|ml|dl|l|g|kg)\b", re.IGNORECASE)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def absolute_url(base_url: str, href: str | None) -> str:
    return urljoin(base_url, href or "")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def extract_product_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = {
        canonicalize_url(absolute_url(base_url, link.get("href")))
        for link in soup.select("li.product-item a.product-teaser__image[href]")
    }
    return sorted(url for url in urls if url.startswith(base_url.rstrip("/")))


def extract_next_listing_url(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    link = soup.select_one("li.pages-item-next a[href]")
    return absolute_url(base_url, link.get("href")) if link else ""


def extract_listing_product_total(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    amount = soup.select_one(".toolbar-amount")
    if not amount:
        return 0
    match = re.search(r"von\s+([\d.'’]+)", amount.get_text(" ", strip=True), re.IGNORECASE)
    return int(re.sub(r"\D", "", match.group(1))) if match else 0


def _extract_breadcrumb_path(soup: BeautifulSoup) -> str:
    values: list[str] = []
    for item in soup.select(".breadcrumbs li"):
        if "product" in (item.get("class") or []):
            continue
        value = normalize_space(item.get_text(" ", strip=True))
        if value and value != "Shop":
            values.append(value)
    return " > ".join(values)


def _extract_attributes(soup: BeautifulSoup) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for item in soup.select(".product-detail__attributes > ul > li"):
        overlay = item.select_one(".overlay-flag > span")
        if not overlay:
            continue
        label = normalize_space(overlay.get_text(" ", strip=True))
        if not label:
            continue
        content_nodes = item.select("span.content")
        if content_nodes:
            values = [normalize_space(node.get_text(" ", strip=True)) for node in content_nodes]
        else:
            overlay.parent.decompose()
            for icon in item.select("svg"):
                icon.decompose()
            values = [normalize_space(item.get_text(" ", strip=True))]
        value = " | ".join(dict.fromkeys(value for value in values if value))
        if value:
            attributes[label] = value
    return attributes


def _extract_further_information(soup: BeautifulSoup) -> dict[str, str]:
    details: dict[str, str] = {}
    for item in soup.select(".further-info__item"):
        title = item.select_one(".further-info__title")
        content = item.select_one(".further-info__content")
        if not title or not content:
            continue
        key = normalize_space(title.get_text(" ", strip=True))
        value = normalize_space(content.get_text(" ", strip=True))
        if key and value:
            details[key] = value
    return details


def _extract_vessel(variant_labels: list[str]) -> tuple[str, str]:
    for label in variant_labels:
        match = PACKAGING_RE.search(label)
        if not match:
            continue
        size = match.group("size").replace(",", ".")
        try:
            size = f"{float(size):.4f}".rstrip("0").rstrip(".")
        except ValueError:
            pass
        return size, match.group("unit").lower()
    return "", ""


def parse_product_record(html: str, product_url: str) -> NormalizedProduct | None:
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one("h1 .base, h1")
    title = normalize_space(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title:
        return None

    producer = soup.select_one(".producer__name")
    manufacturer = normalize_space(producer.get_text(" ", strip=True)) if producer else ""
    form = soup.select_one("form[data-product-sku]")
    sku = normalize_space(form.get("data-product-sku", "")) if form else ""
    price = soup.select_one('[data-price-type="finalPrice"][data-price-amount]')
    price_value = normalize_space(price.get("data-price-amount", "")) if price else ""
    currency = "CHF" if price_value else ""
    image = soup.select_one('meta[property="og:image"], .product__detail__image img[src]')
    image_url = absolute_url(product_url, image.get("content") or image.get("src")) if image else ""
    variants = [normalize_space(node.get_text(" ", strip=True)) for node in soup.select("a[data-simple-product-id]")]
    vessel_size, vessel_unit = _extract_vessel(variants)
    attributes = _extract_attributes(soup)
    further_information = _extract_further_information(soup)
    origin = attributes.get("Herkunft", "")
    origin_parts = [normalize_space(value) for value in origin.split("|") if normalize_space(value)]
    region = origin_parts[0] if origin_parts else ""
    country = origin_parts[-1] if len(origin_parts) > 1 else ""
    meta_description = soup.select_one('meta[name="description"]')
    description_parts = [
        f"{label}: {value}"
        for label, value in further_information.items()
        if value
    ]
    if not description_parts and meta_description and meta_description.get("content"):
        description_parts.append(normalize_space(meta_description["content"]))
    specs = {
        re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_"): value
        for label, value in {**attributes, **further_information}.items()
    }
    if variants:
        specs["available_bottle_sizes"] = " | ".join(dict.fromkeys(variants))

    return NormalizedProduct(
        product_url=product_url,
        canonical_url=canonicalize_url(product_url),
        category_path=_extract_breadcrumb_path(soup),
        product_name=title,
        item_name=title,
        sku=sku,
        price=price_value,
        currency=currency,
        image_url=image_url,
        description="\n\n".join(description_parts),
        manufacturer=manufacturer,
        brand=manufacturer,
        region=region,
        country=country,
        labels=[attributes["Typ"]] if attributes.get("Typ") else [],
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        specs=specs,
    )
