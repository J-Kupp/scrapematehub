from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
from dataclasses import asdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from models import NormalizedProduct, SupplierConfig, SyncSummary


ALLOWED_LABELS = {
    "NEW",
    "BIO",
    "SEASONAL",
    "DISCOUNTED",
    "VEGAN",
    "VEGETARIAN",
    "GLUTEN_FREE",
}

VESSEL_TYPE_CODES = {
    "bag": "BG",
    "bottle": "BO",
    "bowl": "BM",
    "can": "BI",
    "canister": "CI",
    "box": "BX",
    "container": "WA",
    "cup": "CU",
    "cutlery": "NA",
    "lid": "NA",
    "napkin": "NA",
    "pack": "PK",
    "plate": "PU",
    "roll": "RO",
    "straw": "NA",
}

BUNDLE_TYPE_CODES = {
    "beutel": "BG",
    "box": "BX",
    "karton": "CT",
    "pack": "PK",
    "rolle": "RO",
    "set": "PK",
}


class YbmApiError(RuntimeError):
    pass


COMMON_CA_BUNDLE_PATHS = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)


def slugify(value: str, *, fallback: str = "item") -> str:
    lowered = value.strip().lower()
    lowered = lowered.replace("&", "and")
    slug = re.sub(r"[^a-z0-9_-]+", "-", lowered).strip("-")
    return slug or fallback


def stable_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def source_identifier_for_product(product: NormalizedProduct) -> str:
    if product.sku:
        return product.sku
    variant_key = json.dumps(product.variant_options, sort_keys=True, ensure_ascii=False)
    return f"{product.canonical_url}|{variant_key}"


def build_product_id(product: NormalizedProduct, supplier_slug: str) -> str:
    source_identifier = source_identifier_for_product(product)
    return f"{supplier_slug}__{slugify(source_identifier)}__{stable_hash(source_identifier)}"


def build_category_id(category_path: str, supplier_slug: str) -> str:
    source = category_path or "Uncategorized"
    return f"{supplier_slug}__cat__{slugify(source, fallback='uncategorized')}__{stable_hash(source)}"


def shorten_category_name(category_path: str, *, max_length: int = 64) -> str:
    source = (category_path or "Uncategorized").strip()
    if len(source) <= max_length:
        return source
    segments = [segment.strip() for segment in source.split(">") if segment.strip()]
    if not segments:
        return source[:max_length].rstrip()
    for width in range(1, len(segments) + 1):
        candidate = " > ".join(segments[-width:])
        if len(candidate) <= max_length:
            return candidate
    return segments[-1][:max_length].rstrip()


def build_category_name_map(category_paths: list[str], *, max_length: int = 64) -> dict[str, str]:
    normalized_paths = [path or "Uncategorized" for path in category_paths]
    segments_by_path = {
        path: [segment.strip() for segment in path.split(">") if segment.strip()] or ["Uncategorized"]
        for path in normalized_paths
    }
    chosen_width: dict[str, int] = {path: 1 for path in normalized_paths}

    def candidate(path: str) -> str:
        segments = segments_by_path[path]
        width = min(chosen_width[path], len(segments))
        text = " > ".join(segments[-width:])
        if len(text) <= max_length:
            return text
        return shorten_category_name(path, max_length=max_length)

    while True:
        groups: dict[str, list[str]] = {}
        for path in normalized_paths:
            groups.setdefault(candidate(path), []).append(path)
        collisions = [paths for paths in groups.values() if len(paths) > 1]
        if not collisions:
            break
        progressed = False
        for paths in collisions:
            for path in paths:
                if chosen_width[path] < len(segments_by_path[path]):
                    chosen_width[path] += 1
                    progressed = True
        if not progressed:
            break

    assigned: dict[str, str] = {}
    used: dict[str, str] = {}
    for path in sorted(set(normalized_paths)):
        name = candidate(path)
        if name in used and used[name] != path:
            suffix = stable_hash(path)[:6]
            base = name[: max_length - 7].rstrip(" -")
            name = f"{base}-{suffix}"
        used[name] = path
        assigned[path] = name
    return assigned


def parse_number(value: str) -> int | float | None:
    if not value:
        return None
    cleaned = value.replace("'", "").replace(",", ".").strip()
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return number


def is_integer_like(value: int | float | None) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return True
    return value.is_integer()


def build_vessel_payload(
    *,
    vessel_id: str,
    vessel_size: int | float | None,
    vessel_unit: str,
    vessel_type: str,
) -> dict[str, Any] | None:
    if vessel_size is None or not vessel_unit:
        return None
    if vessel_unit == "g":
        if not is_integer_like(vessel_size):
            return None
        vessel_size = int(vessel_size)
    return {
        "id": vessel_id,
        "size": vessel_size,
        "unit": vessel_unit,
        "type": vessel_type,
    }


def build_bundle_payload(
    *,
    bundle_id: str,
    bundle_size: int | float | None,
    bundle_type: str,
) -> dict[str, Any] | None:
    if bundle_size is None or not bundle_type:
        return None
    if not is_integer_like(bundle_size):
        return None
    bundle_size = int(bundle_size)
    if bundle_size < 2:
        return None
    return {
        "id": bundle_id,
        "type": bundle_type,
        "size": bundle_size,
    }


def price_to_cents(price: str) -> int | None:
    if not price:
        return None
    cleaned = price.replace("'", "").replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        number = Decimal(cleaned)
    except Exception:
        return None
    return int((number * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def map_vessel_type_code(vessel_type: str) -> str:
    return VESSEL_TYPE_CODES.get(vessel_type.lower(), "NA") if vessel_type else "NA"


def map_bundle_type_code(bundle_type: str) -> str:
    normalized = bundle_type.strip().lower()
    if not normalized:
        return "PK"
    return BUNDLE_TYPE_CODES.get(normalized, "PK")


def compact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        compacted: dict[str, Any] = {}
        for key, value in payload.items():
            compact_value = compact_payload(value)
            if compact_value in ("", None, [], {}):
                continue
            compacted[key] = compact_value
        return compacted
    if isinstance(payload, list):
        compacted_list = [compact_payload(item) for item in payload]
        compacted_list = [item for item in compacted_list if item not in ("", None, [], {})]
        return compacted_list
    return payload


def resolve_ca_bundle_path() -> str | None:
    explicit = os.environ.get("YBM_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if explicit and Path(explicit).is_file():
        return explicit

    try:
        import certifi  # type: ignore
    except ImportError:
        certifi = None

    if certifi is not None:
        bundle = certifi.where()
        if bundle and Path(bundle).is_file():
            return bundle

    for candidate in COMMON_CA_BUNDLE_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def build_ssl_context_for_url(url: str) -> ssl.SSLContext | None:
    if parse.urlsplit(url).scheme.lower() != "https":
        return None
    bundle = resolve_ca_bundle_path()
    if bundle:
        return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


def build_custom_properties(product: NormalizedProduct) -> dict[str, Any]:
    properties = {
        "manufacturer": product.manufacturer,
        "brand": product.brand,
        "region": product.region,
        "description": product.description,
        "ingredients": "",
        "allergens": [],
        "color": product.color,
        "storage_advice": "",
        "labels": sorted(label for label in product.labels if label in ALLOWED_LABELS),
    }
    country = product.country.strip().upper()
    if len(country) == 2 and country.isalpha():
        properties["country"] = country
    return compact_payload(properties)


def map_product_to_ybm(product: NormalizedProduct, supplier_slug: str) -> dict[str, Any]:
    product_id = build_product_id(product, supplier_slug)
    category_id = build_category_id(product.category_path, supplier_slug)
    payload: dict[str, Any] = {
        "id": product_id,
        "status": product.status,
        "category": category_id,
        "name": re.sub(r"\s+", " ", product.item_name).strip()[:64],
        "order_by": product.order_by or "vessel",
    }
    vessel_size = parse_number(product.vessel_size)
    vessel_payload = build_vessel_payload(
        vessel_id=f"{product_id}__vessel",
        vessel_size=vessel_size,
        vessel_unit=product.vessel_unit,
        vessel_type=map_vessel_type_code(product.vessel_type),
    )
    if vessel_payload:
        payload["vessel"] = vessel_payload
    bundle_size = parse_number(product.bundle_size)
    bundle_payload = build_bundle_payload(
        bundle_id=f"{product_id}__bundle",
        bundle_size=bundle_size,
        bundle_type=map_bundle_type_code(product.bundle_type),
    )
    if bundle_payload:
        payload["bundles"] = [bundle_payload]
    price_cents = price_to_cents(product.price)
    if price_cents is not None:
        payload["price"] = price_cents
    if product.price_per:
        payload["price_per"] = product.price_per
    min_order_count = parse_number(product.min_order_count)
    if min_order_count is not None:
        payload["min_order_count"] = min_order_count
    if product.gtin:
        payload["gtin"] = product.gtin
    custom_properties = build_custom_properties(product)
    if custom_properties:
        payload["custom_properties"] = custom_properties
    if product.image_url:
        payload["image"] = {"url": product.image_url}
    return compact_payload(payload)


def map_row_to_ybm(row: dict[str, str], supplier_slug: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row["Item ID"],
        "status": row["Status"],
        "category": build_category_id(row["Category name"], supplier_slug),
        "name": re.sub(r"\s+", " ", row["Item name"]).strip()[:64],
        "order_by": row["Order by"] or "vessel",
    }
    vessel_size = parse_number(row["Vessel size"])
    vessel_payload = build_vessel_payload(
        vessel_id=f"{row['Item ID']}__vessel",
        vessel_size=vessel_size,
        vessel_unit=row["Vessel unit"],
        vessel_type=row["Vessel type"] or "NA",
    )
    if vessel_payload:
        payload["vessel"] = vessel_payload
    bundle_size = parse_number(row["Bundle size"])
    bundle_payload = build_bundle_payload(
        bundle_id=f"{row['Item ID']}__bundle",
        bundle_size=bundle_size,
        bundle_type=row["Bundle type"],
    )
    if bundle_payload:
        payload["bundles"] = [bundle_payload]
    price_cents = price_to_cents(row["Price"])
    if price_cents is not None:
        payload["price"] = price_cents
    if row["Price per"]:
        payload["price_per"] = row["Price per"]
    min_order_count = parse_number(row["Minimum order count"])
    if min_order_count is not None:
        payload["min_order_count"] = min_order_count
    if row["GTIN"]:
        payload["gtin"] = row["GTIN"]
    custom_properties = compact_payload(
        {
            "manufacturer": row["Manufacturer"],
            "brand": row["Brand"],
            "region": row["Region"],
            "description": row["Description"],
            "ingredients": row["Ingredients"],
            "allergens": [item.strip() for item in row["Allergens"].split(",") if item.strip()],
            "color": row["Color"],
            "storage_advice": row["Storage advice"],
            "labels": [label.strip() for label in row["Labels"].split(",") if label.strip() in ALLOWED_LABELS],
            "country": row["Country"].strip().upper() if len(row["Country"].strip()) == 2 else "",
        }
    )
    if custom_properties:
        payload["custom_properties"] = custom_properties
    if row["Image"]:
        payload["image"] = {"url": row["Image"]}
    return compact_payload(payload)


def map_categories(products: list[NormalizedProduct], supplier_slug: str) -> list[dict[str, str]]:
    category_name_map = build_category_name_map([product.category_path or "Uncategorized" for product in products])
    categories: dict[str, dict[str, str]] = {}
    for product in products:
        category_path = product.category_path or "Uncategorized"
        category_id = build_category_id(category_path, supplier_slug)
        categories[category_id] = {"id": category_id, "name": category_name_map[category_path]}
    return sorted(categories.values(), key=lambda item: item["id"])


def map_categories_from_rows(rows: list[dict[str, str]], supplier_slug: str) -> list[dict[str, str]]:
    category_name_map = build_category_name_map([row["Category name"] or "Uncategorized" for row in rows])
    categories: dict[str, dict[str, str]] = {}
    for row in rows:
        category_path = row["Category name"] or "Uncategorized"
        category_id = build_category_id(category_path, supplier_slug)
        categories[category_id] = {"id": category_id, "name": category_name_map[category_path]}
    return sorted(categories.values(), key=lambda item: item["id"])


def reconcile_categories(
    local_categories: list[dict[str, str]],
    remote_categories: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    remote_by_id = {str(category.get("id", "")): category for category in remote_categories}
    remote_by_name = {
        str(category.get("name", "")).strip().casefold(): category
        for category in remote_categories
        if category.get("name")
    }
    categories_to_sync: list[dict[str, str]] = []
    category_aliases: dict[str, str] = {}
    for category in local_categories:
        local_id = category["id"]
        remote = remote_by_id.get(local_id)
        if remote is not None:
            category_aliases[local_id] = local_id
            categories_to_sync.append(category)
            continue
        remote_by_name_match = remote_by_name.get(category["name"].strip().casefold())
        if remote_by_name_match is not None:
            category_aliases[local_id] = str(remote_by_name_match["id"])
            continue
        category_aliases[local_id] = local_id
        categories_to_sync.append(category)
    return categories_to_sync, category_aliases


def remap_payload_categories(
    payloads: dict[str, dict[str, Any]],
    category_aliases: dict[str, str],
) -> dict[str, dict[str, Any]]:
    remapped: dict[str, dict[str, Any]] = {}
    for product_id, payload in payloads.items():
        updated = dict(payload)
        category_id = str(updated.get("category", ""))
        if category_id in category_aliases:
            updated["category"] = category_aliases[category_id]
        remapped[product_id] = updated
    return remapped


def canonicalize_local_payload(payload: dict[str, Any]) -> dict[str, Any]:
    comparable = dict(payload)
    comparable.pop("id", None)
    if "image" in comparable and isinstance(comparable["image"], dict):
        comparable["image"] = {"url": comparable["image"].get("url")}
    if "bundles" in comparable:
        comparable["bundles"] = sorted(comparable["bundles"], key=lambda item: item.get("id", ""))
    if "custom_properties" in comparable and "labels" in comparable["custom_properties"]:
        comparable["custom_properties"]["labels"] = sorted(comparable["custom_properties"]["labels"])
    return compact_payload(comparable)


def canonicalize_remote_product(remote_product: dict[str, Any]) -> dict[str, Any]:
    comparable = {
        "status": remote_product.get("status"),
        "category": remote_product.get("category"),
        "name": remote_product.get("name"),
        "order_by": remote_product.get("order_by"),
        "vessel": remote_product.get("vessel"),
        "bundles": remote_product.get("bundles", []),
        "price": remote_product.get("price"),
        "price_per": remote_product.get("price_per"),
        "min_order_count": remote_product.get("min_order_count"),
        "gtin": remote_product.get("gtin"),
        "custom_properties": remote_product.get("custom_properties", {}),
    }
    image = remote_product.get("image")
    if isinstance(image, dict) and image.get("url"):
        comparable["image"] = {"url": image["url"]}
    if "bundles" in comparable:
        comparable["bundles"] = sorted(comparable["bundles"], key=lambda item: item.get("id", ""))
    if "custom_properties" in comparable and "labels" in comparable["custom_properties"]:
        comparable["custom_properties"]["labels"] = sorted(comparable["custom_properties"]["labels"])
    return compact_payload(comparable)


class YbmSyncClient:
    def __init__(self, api_base: str, token: str, timeout_seconds: int = 60) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{parse.urlencode(query)}"
        data = None
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=method)
        context = build_ssl_context_for_url(url)
        try:
            if context is None:
                response_cm = request.urlopen(req, timeout=self.timeout_seconds)
            else:
                response_cm = request.urlopen(req, timeout=self.timeout_seconds, context=context)
            with response_cm as response:
                body = response.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise YbmApiError(f"{method} {path} failed with {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise YbmApiError(f"{method} {path} failed: {exc.reason}") from exc
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def list_categories(self) -> list[dict[str, Any]]:
        return self._request_json("GET", "/categories").get("categories", [])

    def create_category(self, category: dict[str, str]) -> dict[str, Any]:
        return self._request_json("POST", "/categories", payload=category)

    def update_category(self, category_id: str, category: dict[str, str]) -> dict[str, Any]:
        return self._request_json("PUT", f"/categories/{parse.quote(category_id, safe='')}", payload=category)

    def list_products(self) -> list[dict[str, Any]]:
        cursor = ""
        products: list[dict[str, Any]] = []
        while True:
            query = {"cursor": cursor} if cursor else None
            response = self._request_json("GET", "/products", query=query)
            products.extend(response.get("products", []))
            cursor = response.get("cursor", "")
            if not cursor:
                return products

    def create_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", "/products", payload=payload)

    def patch_product(self, product_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.pop("id", None)
        return self._request_json("PATCH", f"/products/{parse.quote(product_id, safe='')}", payload=body)


def require_token(config: SupplierConfig) -> str:
    token = os.environ.get(config.ybm_token_env_var, "").strip()
    if not token:
        raise YbmApiError(
            f"Missing YourBarMate token in environment variable {config.ybm_token_env_var}. "
            "Load it via .env.local or your shell before running sync."
        )
    return token


def sync_to_ybm(
    config: SupplierConfig,
    products: list[NormalizedProduct],
    *,
    dry_run: bool = False,
    limit_products: int | None = None,
    skip_inactivate: bool = False,
) -> tuple[SyncSummary, list[dict[str, Any]], list[dict[str, Any]]]:
    token = require_token(config)
    client = YbmSyncClient(config.ybm_api_base, token)
    summary = SyncSummary(supplier_slug=config.supplier_slug, dry_run=dry_run)

    remote_categories = client.list_categories()
    remote_products = client.list_products()
    remote_products_by_id = {product["id"]: product for product in remote_products}
    owned_remote_product_ids = {
        product_id
        for product_id in remote_products_by_id
        if product_id.startswith(f"{config.supplier_slug}__")
    }
    summary.old_catalog_products = len(owned_remote_product_ids)

    local_categories = map_categories(products, config.supplier_slug)
    categories_to_sync, category_aliases = reconcile_categories(local_categories, remote_categories)
    remote_categories_by_id = {str(category["id"]): category for category in remote_categories}
    for category in categories_to_sync:
        remote = remote_categories_by_id.get(category["id"])
        if remote is None:
            summary.created_categories += 1
            if not dry_run:
                client.create_category(category)
            continue
        if remote.get("name") != category["name"]:
            summary.updated_categories += 1
            if not dry_run:
                client.update_category(category["id"], category)

    local_payloads = {
        payload["id"]: payload
        for payload in (map_product_to_ybm(product, config.supplier_slug) for product in products)
    }
    if limit_products is not None:
        local_payloads = dict(sorted(local_payloads.items())[:limit_products])
    local_payloads = remap_payload_categories(local_payloads, category_aliases)
    for product_id, payload in sorted(local_payloads.items()):
        remote = remote_products_by_id.get(product_id)
        if remote is None:
            summary.created_products += 1
            if not dry_run:
                client.create_product(payload)
            continue
        local_comparable = canonicalize_local_payload(payload)
        remote_comparable = canonicalize_remote_product(remote)
        if local_comparable == remote_comparable:
            summary.unchanged_products += 1
            continue
        summary.updated_products += 1
        if not dry_run:
            client.patch_product(product_id, payload)

    if not skip_inactivate:
        missing_remote_product_ids = owned_remote_product_ids - set(local_payloads)
        for product_id in sorted(missing_remote_product_ids):
            remote = remote_products_by_id[product_id]
            if remote.get("status") == "INACTIVE":
                summary.unchanged_products += 1
                continue
            summary.inactivated_products += 1
            if not dry_run:
                client.patch_product(product_id, {"status": "INACTIVE"})

    return summary, remote_categories, remote_products


def sync_rows_to_ybm(
    config: SupplierConfig,
    rows: list[dict[str, str]],
    *,
    dry_run: bool = False,
    limit_products: int | None = None,
    skip_inactivate: bool = False,
) -> tuple[SyncSummary, list[dict[str, Any]], list[dict[str, Any]]]:
    token = require_token(config)
    client = YbmSyncClient(config.ybm_api_base, token)
    summary = SyncSummary(supplier_slug=config.supplier_slug, dry_run=dry_run)

    remote_categories = client.list_categories()
    remote_products = client.list_products()
    remote_products_by_id = {product["id"]: product for product in remote_products}
    owned_remote_product_ids = {
        product_id
        for product_id in remote_products_by_id
        if product_id.startswith(f"{config.supplier_slug}__")
    }
    summary.old_catalog_products = len(owned_remote_product_ids)

    local_categories = map_categories_from_rows(rows, config.supplier_slug)
    categories_to_sync, category_aliases = reconcile_categories(local_categories, remote_categories)
    remote_categories_by_id = {str(category["id"]): category for category in remote_categories}
    for category in categories_to_sync:
        remote = remote_categories_by_id.get(category["id"])
        if remote is None:
            summary.created_categories += 1
            if not dry_run:
                client.create_category(category)
            continue
        if remote.get("name") != category["name"]:
            summary.updated_categories += 1
            if not dry_run:
                client.update_category(category["id"], category)

    local_payloads = {payload["id"]: payload for payload in (map_row_to_ybm(row, config.supplier_slug) for row in rows)}
    if limit_products is not None:
        local_payloads = dict(sorted(local_payloads.items())[:limit_products])
    local_payloads = remap_payload_categories(local_payloads, category_aliases)

    for product_id, payload in sorted(local_payloads.items()):
        remote = remote_products_by_id.get(product_id)
        if remote is None:
            summary.created_products += 1
            if not dry_run:
                client.create_product(payload)
            continue
        if canonicalize_local_payload(payload) == canonicalize_remote_product(remote):
            summary.unchanged_products += 1
            continue
        summary.updated_products += 1
        if not dry_run:
            client.patch_product(product_id, payload)

    if not skip_inactivate:
        missing_remote_product_ids = owned_remote_product_ids - set(local_payloads)
        for product_id in sorted(missing_remote_product_ids):
            remote = remote_products_by_id[product_id]
            if remote.get("status") == "INACTIVE":
                summary.unchanged_products += 1
                continue
            summary.inactivated_products += 1
            if not dry_run:
                client.patch_product(product_id, {"status": "INACTIVE"})

    return summary, remote_categories, remote_products
