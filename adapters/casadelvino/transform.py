from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from models import NormalizedProduct

COUNTRY_CODES = {
    "Argentinien": "AR", "Australien": "AU", "Chile": "CL", "Deutschland": "DE",
    "Frankreich": "FR", "Grossbritannien": "GB", "Italien": "IT", "Österreich": "AT",
    "Portugal": "PT", "Schweiz": "CH", "Spanien": "ES", "Südafrika": "ZA",
    "Thailand": "TH", "United States of America": "US",
}
CATEGORY_NAMES = {
    "grappa": "Grappa", "glaswaren": "Glaswaren", "spirituosen": "Spirituosen",
    "rosewein": "Roséwein", "rotwein": "Rotwein", "weisswein": "Weisswein",
}
MULTIPACK = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(ml|cl|dl|l)\b", re.I)
CARTON = re.compile(r"\b(Carton|Karton|OHK|Holzkiste)\s*(\d+)\s*er\b", re.I)


def text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def number(value: Any, *, allow_zero: bool = False) -> str:
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        return ""
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        return ""
    return format(amount.normalize(), "f")


def parse_search_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        match = re.search(r"Components\.Artikelsuche,\s*(\{)", script.string or "")
        if match:
            payload, _ = json.JSONDecoder().raw_decode(script.string[match.start(1):])
            return payload["searchResult"]
    raise ValueError("Casa del Vino search data was not found in the public page")


def listing_item_id(row: dict[str, Any]) -> str:
    return number(row.get("artikelId"))


def detail_url(row: dict[str, Any], base: str) -> str:
    url = urljoin(base, text(row.get("urlArtikelDetail")))
    scope = urlsplit(base)
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc) != (scope.scheme, scope.netloc) or not re.fullmatch(r"/de/shop/[^/]+/[^/]+", parts.path):
        return ""
    return parts._replace(query="", fragment="").geturl()


def _product_schema(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or "")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("@type") == "Product":
            return value
    return {}


def _detail_fields(soup: BeautifulSoup) -> tuple[dict[str, str], dict[str, str]]:
    fields, reviews = {}, {}
    for section in soup.select(".artikel-infos .uppercase-titel"):
        heading = text(section.get_text(" ", strip=True))
        if heading not in {"Alle Details", "Bewertungen"}:
            continue
        target = reviews if heading == "Bewertungen" else fields
        for label in section.parent.select(".col-5"):
            value = label.find_next_sibling(class_="col-7")
            key = text(label.get_text(" ", strip=True)).rstrip(":")
            if key and value:
                content = text(value.get_text(" / " if key == "Passt zu" else " ", strip=True))
                if content:
                    target[key] = f"{target[key]} / {content}" if key in target else content
    return fields, reviews


def _packaging(listing: dict[str, Any], fields: dict[str, str]) -> dict[str, str]:
    sale_unit = text(listing.get("bestelleinheitAbfuellung"))
    volume = number(listing.get("inhaltCl"))
    if not volume:
        return {"vessel_size": "1", "vessel_unit": "quantity"}
    result = {"vessel_size": volume, "vessel_unit": "cl", "vessel_type": "BO"}
    pack = MULTIPACK.fullmatch(sale_unit)
    container = CARTON.search(fields.get("Gebinde", ""))
    container_type = "CT" if container and container[1].lower() in {"carton", "karton"} else "BX"
    if pack and int(pack[1]) > 1 and number(pack[2]):
        result.update(vessel_size=number(pack[2]), vessel_unit=pack[3].lower(),
                      bundle_size=pack[1], bundle_type=container_type if container else "PK")
    elif not pack and container and int(container[2]) > 1:
        result.update(bundle_size=container[2], bundle_type=container_type)
    return result


def parse_product_record(html: str, listing: dict[str, Any], url: str) -> NormalizedProduct:
    soup = BeautifulSoup(html, "lxml")
    schema = _product_schema(soup)
    sku = number(listing.get("artikelnummer"))
    sku_node = soup.select_one(".artikel-infos .artikelnummer")
    detail_sku = text(schema.get("sku")) or (
        text(sku_node.get_text(" ", strip=True).partition(":")[2]) if sku_node else ""
    )
    name = text(listing.get("bezeichnung"))
    if not sku or not name or not detail_sku or sku != detail_sku:
        raise ValueError(f"Listing and product detail identity do not match for SKU {sku}")
    category_key = urlsplit(url).path.split("/")[3]
    category = text(listing.get("eGruppe")) or CATEGORY_NAMES.get(category_key, category_key.replace("-", " ").capitalize())
    fields, reviews = _detail_fields(soup)
    descriptions: dict[str, str] = {}
    for heading in soup.select(".artikel-infos h2"):
        node = heading.find_next_sibling()
        if node and "description" in (node.get("class") or []):
            label = text(heading.get_text(" ", strip=True))
            value = text(node.get_text(" ", strip=True))
            if label and value:
                descriptions[label] = value

    sheet = soup.select_one('.artikel-infos a[href*="/factsheetohnepreis/"]')
    sheet_url = urljoin(url, sheet["href"]) if sheet else ""
    image = schema.get("image") or listing.get("urlListImage")
    if isinstance(image, list):
        image = next(iter(image), "")
    gtin = text(next((schema.get(key) for key in ("gtin13", "gtin14", "gtin12", "gtin8") if schema.get(key)), ""))
    if not re.fullmatch(r"(?:\d{8}|\d{12,14})", gtin) or not gtin.strip("0"):
        gtin = ""
    country = text(listing.get("land"))
    if country == "Ohne Länderbezeichnung":
        country = ""
    vintage = number(listing.get("jahrgang"))
    if not re.fullmatch(r"\d{4}", vintage):
        vintage = ""
    alcohol = fields.get("Alkoholgehalt", "")
    if not alcohol and (volume := number(listing.get("volumen"), allow_zero=True)):
        alcohol = f"{volume}%"

    offer = schema.get("offers") or {}
    if not isinstance(offer, dict):
        offer = {}
    price = number(offer.get("price")) if offer.get("priceCurrency") == "CHF" else ""
    listing_price = number(listing.get("preis"))
    price_node = soup.select_one(".artikel-infos .price-wrapper .price")
    visible_price = number(price_node.get_text(strip=True).replace("\u2019", "").replace("'", "")) if price_node else ""
    current = visible_price or price or listing_price
    producer = text(listing.get("produzent"))
    options = _packaging(listing, fields)
    multipack = MULTIPACK.fullmatch(text(listing.get("bestelleinheitAbfuellung")))
    bundle_gtin = ""
    if multipack and options.get("bundle_size"):
        bundle_gtin, gtin = gtin, ""
    organic = bool(soup.select_one(".artikel-infos .icon-bio"))
    vegan = bool(soup.select_one(".artikel-infos .icon-vegan"))
    labels = ["BIO"] if organic else []
    if listing.get("istAktion"):
        labels.append("DISCOUNTED")
    availability_node = soup.select_one(".artikel-infos .lagerbestand")
    availability = text(availability_node.get_text(" ", strip=True)) if availability_node else ""
    specs = {**descriptions, **fields, **{f"Bewertung {key}": val for key, val in reviews.items()}}
    specs.update({
        "shop_article_id": listing_item_id(listing),
        "public_shop_price_chf": current,
        "public_listing_price_chf": listing_price,
        "public_offer_price_chf": price,
        "public_visible_price_chf": visible_price,
        "public_regular_price_chf": number(listing.get("preisOhneAktion")),
        "public_promotion": str(bool(listing.get("istAktion"))),
        "nettoartikel_shop_flag": str(bool(listing.get("istNetto"))),
        "sale_unit": text(listing.get("bestelleinheitAbfuellung")),
        "public_availability": availability,
        "public_offer_availability": text(offer.get("availability")),
    })
    for link in soup.select('link[rel="alternate"][hreflang][href]'):
        language = link["hreflang"].lower().split("-")[0]
        if language in {"fr", "it", "en"}:
            specs[f"product_url_{language}"] = urljoin(url, link["href"])
    description_items = {
        **descriptions, **fields, **{f"Bewertung {k}": v for k, v in reviews.items()},
        "Produzent": producer, "Herkunft": country, "Region": text(listing.get("region")),
        "Jahrgang": vintage, "Rebsorten": text(listing.get("traubensorte")),
        "Alkoholgehalt": alcohol, "Factsheet": sheet_url,
        "Ernährungsmerkmal": "Vegan" if vegan else "",
    }
    allergens = re.sub(r"^enthält\s+", "", fields.get("Allergiker-Info", ""), flags=re.I)
    status = "OUT_OF_STOCK" if not listing.get("aufAnfrage") and offer.get("availability") == "https://schema.org/OutOfStock" else "ACTIVE"
    return NormalizedProduct(
        product_url=url, canonical_url=url, category_path=category, product_name=name,
        item_name=name, sku=sku, gtin=gtin, bundle_gtin=bundle_gtin, currency="CHF",
        status=status, image_url=urljoin(url, image) if image else "", product_sheet_url=sheet_url,
        description="\n".join(f"{key}: {value}" for key, value in description_items.items() if value),
        manufacturer=producer, brand=producer, country=COUNTRY_CODES.get(country, country),
        region=text(listing.get("region")), vintage=vintage, alcohol_content=alcohol,
        grape_variety=text(listing.get("traubensorte")), wine_making=descriptions.get("Vinifikation / Ausbau", ""),
        allergens=allergens, dietary_labels="Vegan" if vegan else "",
        labels=labels, raw_availability_text=availability,
        packaging_mode="structured_supplier_units", packaging_source="casadelvino_catalog",
        packaging_evidence=" / ".join(filter(None, (text(listing.get("bestelleinheitAbfuellung")), fields.get("Gebinde")))),
        specs={key: value for key, value in specs.items() if value}, **options,
    )
