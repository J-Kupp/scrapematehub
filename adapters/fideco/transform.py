from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from models import NormalizedProduct


PACKAGING_RE = re.compile(r"(?P<size>\d[\d.'’,]*)\s*(?P<unit>kg|g|ml|cl|dl|l)\b", re.IGNORECASE)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def absolute_url(base_url: str, href: str | None) -> str:
    return urljoin(base_url, href or "")


def canonicalize_url(url: str, *, keep_page: bool = False) -> str:
    parsed = urlparse(url)
    query = ""
    if keep_page:
        page = dict(parse_qsl(parsed.query)).get("p", "")
        query = urlencode({"p": page}) if page else ""
    return parsed._replace(query=query, fragment="").geturl()


def product_identifier_from_url(url: str) -> str:
    """Fideco uses the article number as the final path component."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[-1] if parts else ""


def extract_category_links(html: str, base_url: str) -> list[str]:
    """Read all Shopware menu/category paths, but never product detail pages."""
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(base_url).netloc
    urls: set[str] = set()
    for anchor in soup.select("a[href]"):
        url = canonicalize_url(absolute_url(base_url, anchor.get("href")))
        parsed = urlparse(url)
        if parsed.netloc != base_host or not parsed.path.startswith("/Shop/"):
            continue
        # Product detail pages are outside /Shop/; account/search links are not categories.
        if parsed.path.rstrip("/") == "/Shop":
            urls.add(url)
        elif parsed.path.endswith("/"):
            urls.add(url)
    return sorted(urls)


def extract_product_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = {
        canonicalize_url(absolute_url(base_url, anchor.get("href")))
        for anchor in soup.select(".product-box a.product-name[href], .product-box a.product-image-link[href]")
    }
    base_host = urlparse(base_url).netloc
    return sorted(
        url for url in urls
        if urlparse(url).netloc == base_host and not urlparse(url).path.startswith("/Shop/")
    )


def extract_listing_page_count(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    last_page = soup.select_one(".pagination .page-last input[name='p']")
    value = normalize_space(str(last_page.get("value", ""))) if last_page else ""
    return int(value) if value.isdigit() else 1


def listing_page_url(category_url: str, page: int) -> str:
    parsed = urlparse(category_url)
    query = dict(parse_qsl(parsed.query))
    query["p"] = str(page)
    return parsed._replace(query=urlencode(query), fragment="").geturl()


def _text(node) -> str:
    return normalize_space(node.get_text(" ", strip=True)) if node else ""


def _extract_breadcrumb_path(soup: BeautifulSoup) -> str:
    parts = [_text(node) for node in soup.select(".breadcrumb .breadcrumb-title")]
    return " > ".join(part for part in parts if part and part != "Shop")


def _extract_specifications(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for row in soup.select(".product-detail-description-specification-content tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        key, value = _text(cells[0]), _text(cells[1])
        if key and value:
            specs[re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")] = value
    return specs


def _parse_packaging(soup: BeautifulSoup, title: str) -> tuple[str, str, str]:
    evidence = " ".join([title, *(_text(node) for node in soup.select(".product-additional-info-list li"))])
    match = PACKAGING_RE.search(evidence)
    vessel_type = ""
    packaging = soup.select(".product-additional-info-list li")
    if len(packaging) > 1:
        type_match = re.search(r"/\s*(.+)$", _text(packaging[1]))
        vessel_type = normalize_space(type_match.group(1)) if type_match else ""
    if not match:
        return "", "", vessel_type
    size = match.group("size").replace("'", "").replace("’", "").replace(",", ".")
    try:
        size = f"{float(size):.4f}".rstrip("0").rstrip(".")
    except ValueError:
        pass
    return size, match.group("unit").lower(), vessel_type


def parse_product_record(html: str, product_url: str) -> NormalizedProduct | None:
    soup = BeautifulSoup(html, "lxml")
    title = _text(soup.select_one("h1.product-detail-name, h1"))
    if not title:
        return None

    sku_match = re.search(r"Art-Nr\.\s*([\w.-]+)", _text(soup.select_one(".product-box-articlenumber")))
    sku = sku_match.group(1) if sku_match else product_identifier_from_url(product_url)
    image = soup.select_one(".gallery-slider-image[data-full-image], meta[property='og:image']")
    image_url = ""
    if image:
        image_url = absolute_url(product_url, image.get("data-full-image") or image.get("content") or image.get("src"))
    manufacturer_link = soup.select_one(".product-detail-manufacturer-link")
    manufacturer = _text(manufacturer_link)
    description = _text(soup.select_one(".product-detail-description-text"))
    meta_description = soup.select_one("meta[property='og:description'], meta[name='description']")
    if not description and meta_description:
        description = normalize_space(meta_description.get("content", ""))
    specs = _extract_specifications(soup)
    vessel_size, vessel_unit, vessel_type = _parse_packaging(soup, title)
    origin = _text(soup.select_one(".product-additional-info__origin"))
    labels = [_text(node) for node in soup.select(".product-certification-icons .product-icon-text")]

    return NormalizedProduct(
        product_url=product_url,
        canonical_url=canonicalize_url(product_url),
        category_path=_extract_breadcrumb_path(soup),
        product_name=title,
        item_name=title,
        sku=sku,
        currency="CHF",
        image_url=image_url,
        description=description,
        manufacturer=manufacturer,
        brand=manufacturer,
        country=origin,
        labels=[label for label in labels if label],
        vessel_size=vessel_size,
        vessel_unit=vessel_unit,
        vessel_type=vessel_type,
        specs=specs,
    )
