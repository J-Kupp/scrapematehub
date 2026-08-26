from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation

from models import CSV_COLUMNS, NormalizedProduct, ValidationResult


ALLOWED_ORDER_BY = {"vessel", "kg"}
ALLOWED_VESSEL_UNITS = {"l", "dl", "cl", "ml", "kg", "g", "quantity", ""}
ALLOWED_PRICE_PER = {"vessel", "l", "kg", "100g", ""}
ALLOWED_STATUS = {"ACTIVE", "INACTIVE", "OUT_OF_STOCK"}
VESSEL_DECIMAL_PLACES = {
    "kg": 3,
    "l": 3,
    "dl": 2,
    "cl": 1,
    "g": 0,
    "ml": 0,
    "quantity": 0,
}


def vessel_size_is_api_valid(value: str, unit: str) -> bool:
    if not value or not unit:
        return True
    decimal_places = VESSEL_DECIMAL_PLACES.get(unit)
    if decimal_places is None:
        return False
    try:
        number = Decimal(value)
    except InvalidOperation:
        return False
    if number <= 0:
        return False
    smallest_supported_value = Decimal(1).scaleb(-decimal_places)
    return number == number.quantize(smallest_supported_value)


def validate_rows(
    rows: list[dict[str, str]],
    discovered_product_urls: set[str],
    failures: list[dict[str, str]],
    covered_product_url_count: int | None = None,
    products: list[NormalizedProduct] | None = None,
) -> ValidationResult:
    warnings: list[str] = []
    errors: list[str] = []
    if discovered_product_urls and not rows:
        errors.append(
            "No products were exported although product URLs were discovered; refusing an empty catalog sync."
        )
    if rows and list(rows[0].keys()) != CSV_COLUMNS:
        errors.append("CSV columns do not match the required schema order.")

    item_ids = [row["Item ID"] for row in rows]
    duplicates = [item_id for item_id, count in Counter(item_ids).items() if item_id and count > 1]
    if duplicates:
        errors.append(f"Duplicate Item IDs found: {', '.join(sorted(duplicates)[:20])}")

    for row in rows:
        if row["Order by"] not in ALLOWED_ORDER_BY:
            errors.append(f"Invalid Order by value for {row['Item ID']}: {row['Order by']}")
        if row["Vessel unit"] not in ALLOWED_VESSEL_UNITS:
            errors.append(f"Invalid Vessel unit for {row['Item ID']}: {row['Vessel unit']}")
        if not vessel_size_is_api_valid(row["Vessel size"], row["Vessel unit"]):
            errors.append(
                f"Invalid Vessel size for {row['Item ID']}: {row['Vessel size']} {row['Vessel unit']} "
                "does not meet the supported unit precision."
            )
        if row["Price per"] not in ALLOWED_PRICE_PER:
            errors.append(f"Invalid Price per for {row['Item ID']}: {row['Price per']}")
        if row["Status"] not in ALLOWED_STATUS:
            errors.append(f"Invalid Status for {row['Item ID']}: {row['Status']}")
        if row["Price"] and any(symbol in row["Price"] for symbol in "CHF€$"):
            errors.append(f"Currency symbol found in Price for {row['Item ID']}: {row['Price']}")

    if products is not None:
        for product, row in zip(products, rows):
            mode = product.packaging_mode
            if mode == "suspected_contaminated_bundle":
                errors.append(
                    f"Contaminated packaging text suspected for {row['Item ID']}: {product.packaging_evidence}"
                )
            if mode == "unresolved_named_multipack":
                errors.append(
                    f"Named-unit multipack unresolved for {row['Item ID']}: {product.packaging_evidence}"
                )
            if mode == "preserve_inner_unit_divide_price" and not row["Bundle size"]:
                errors.append(f"Missing bundle size after named-unit interpretation for {row['Item ID']}")
            if mode == "preserve_inner_unit_divide_price" and row["Price"] == product.price and not row["Bundle size"]:
                errors.append(f"Bundle-priced single-unit export unresolved for {row['Item ID']}")
            if mode == "flatten_piece_pack" and row["Bundle size"]:
                errors.append(f"Flattened piece-pack still has a bundle for {row['Item ID']}")

    exported_urls = {row["Image"] for row in rows if row["Image"]}
    covered_product_urls = covered_product_url_count if covered_product_url_count is not None else len(rows) + len(failures)
    if covered_product_urls < len(discovered_product_urls):
        warnings.append(
            "Discovered product URLs are not fully covered by exported rows plus failures: "
            f"{covered_product_urls} covered vs {len(discovered_product_urls)} discovered."
        )
    if failures:
        warnings.append(f"{len(failures)} discovered product URLs failed to parse.")
    if not exported_urls:
        warnings.append("No product images were exported.")
    return ValidationResult(
        errors=errors,
        warnings=warnings,
        performed=True,
        row_count=len(rows),
        passed_row_count=len(rows) if not errors else 0,
    )
