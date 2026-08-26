from __future__ import annotations

import csv
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from export import write_csv_rows
from models import CSV_COLUMNS


ALLOWED_ORDER_BY = {"vessel", "kg"}
ALLOWED_VESSEL_UNITS = {"l", "dl", "cl", "ml", "kg", "g", "quantity"}
ALLOWED_PRICE_PER = {"vessel", "l", "kg", "100g"}
VESSEL_DECIMAL_PLACES = {
    "kg": 3,
    "l": 3,
    "dl": 2,
    "cl": 1,
    "g": 0,
    "ml": 0,
    "quantity": 0,
}
ALLOWED_STATUS = {"ACTIVE", "INACTIVE", "OUT_OF_STOCK"}
ALLOWED_BUNDLE_TYPES = {
    "3A", "6H", "AC", "BA", "BC", "BG", "BH", "BI", "BJ", "BK", "BM", "BO", "BX", "CI", "CT", "CU",
    "CY", "DH", "DN", "DR", "GR", "JR", "KG", "PA", "PC", "PK", "PT", "PR", "PU", "RD", "RO", "SA",
    "TU", "TY", "WA", "NA", "ZZ", "PX",
}
ALLOWED_VESSEL_TYPES = ALLOWED_BUNDLE_TYPES - {"PX"}
ALLOWED_LABELS = {"NEW", "BIO", "SEASONAL", "DISCOUNTED"}
ALLOWED_VAT_VALUES = {"2.6", "3.8", "7.7", "8.1"}
CORRECTION_REPORT_COLUMNS = [
    "row_number",
    "item_id_before",
    "item_id_after",
    "field",
    "value_before",
    "value_after",
    "reason",
]
EMPTY_SENTINELS = {"", "-", "n/a", "null", "none"}
TYPE_CODE_PATTERN = re.compile(r"^(?:3A|6H|AC|BA|BC|BG|BH|BI|BJ|BK|BM|BO|BX|CI|CT|CU|CY|DH|DN|DR|GR|JR|KG|PA|PC|PK|PT|PR|PU|RD|RO|SA|TU|TY|WA|NA|ZZ|PX)$")
ITEM_ID_ALLOWED_PATTERN = re.compile(r"[^a-zA-Z0-9.~_%-]+")
ITEM_ID_VALID_PATTERN = re.compile(r"^[a-zA-Z0-9.~_%-]+$")
PRICE_EXTRACT_PATTERN = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
MULTIPACK_PATTERN = re.compile(
    r"(?P<count>\d+)\s*[xX]\s*(?P<size>\d+(?:[.,]\d+)?)\s*(?P<unit>l|liter|litre|dl|cl|ml|kg|kilo|g|gramm|stück|stk|pcs?|pieces?|piece|quantity)\b",
    re.IGNORECASE,
)
SINGLE_SIZE_PATTERN = re.compile(
    r"(?P<size>\d+(?:[.,]\d+)?)\s*(?P<unit>l|liter|litre|dl|cl|ml|kg|kilo|g|gramm|stück|stk|pcs?|pieces?|piece|quantity)\b",
    re.IGNORECASE,
)
BUNDLE_COUNT_PATTERN = re.compile(
    r"(?P<count>\d+)\s*(?:x|stück|stk\.?|pieces?|pcs?)\s*/?\s*(?P<type>karton|carton|box|pack|package|paket|paquet|confezione|tray|plateau|vassoio|palette|pallet|rolle|rouleau|rotolo|beutel|sachet|sacchetto|eimer|seau|secchio|kiste|caisse|cassa|dispokarton|dispenserbox)\b",
    re.IGNORECASE,
)
DIMENSION_LIKE_PATTERN = re.compile(r"^\d+(?:[.,]\d+)?$")

TYPE_SYNONYMS = {
    "bottle": "BO",
    "flasche": "BO",
    "bouteille": "BO",
    "bottiglia": "BO",
    "glass": "GR",
    "glas": "GR",
    "verre": "GR",
    "bicchiere": "GR",
    "jar": "JR",
    "pot": "JR",
    "barattolo": "JR",
    "carton": "CT",
    "karton": "CT",
    "cartone": "CT",
    "dispokarton": "CT",
    "box": "BX",
    "kiste": "BX",
    "caisse": "BX",
    "cassa": "BX",
    "dispenserbox": "BX",
    "pack": "PK",
    "package": "PK",
    "paket": "PK",
    "paquet": "PK",
    "confezione": "PK",
    "tray": "PU",
    "plateau": "PU",
    "vassoio": "PU",
    "pallet": "PX",
    "palette": "PX",
    "bag": "BG",
    "beutel": "BG",
    "sachet": "BG",
    "sacchetto": "BG",
    "sack": "SA",
    "sac": "SA",
    "sacco": "SA",
    "can": "BI",
    "dose": "BI",
    "canette": "BI",
    "lattina": "BI",
    "canister": "CI",
    "kanister": "CI",
    "bidon": "CI",
    "bidone": "CI",
    "jerrican": "3A",
    "cup": "CU",
    "becher": "CU",
    "gobelet": "CU",
    "bucket": "BJ",
    "eimer": "BJ",
    "seau": "BJ",
    "secchio": "BJ",
    "basket": "BK",
    "korb": "BK",
    "panier": "BK",
    "cesto": "BK",
    "barrel": "BA",
    "fass": "BA",
    "baril": "BA",
    "barile": "BA",
    "keg": "KG",
    "fût": "KG",
    "fusto": "KG",
    "drum": "DR",
    "trommel": "DR",
    "tambour": "DR",
    "roll": "RO",
    "rolle": "RO",
    "rouleau": "RO",
    "rotolo": "RO",
    "tube": "TU",
    "tubo": "TU",
    "dispenser": "DN",
    "spender": "DN",
    "distributeur": "DN",
    "distributore": "DN",
    "foil": "PR",
    "folie": "PR",
    "film": "PR",
    "pellicola": "PR",
    "pieces": "NA",
    "piece": "NA",
    "stück": "NA",
    "stk": "NA",
    "pièces": "NA",
    "pezzi": "NA",
    "single": "ZZ",
    "singles": "ZZ",
    "einzeln": "ZZ",
    "bowl": "BM",
    "schale": "BM",
    "plate": "PU",
    "teller": "PU",
    "container": "WA",
    "lid": "NA",
    "deckel": "NA",
    "straw": "NA",
    "trinkhalm": "NA",
    "napkin": "NA",
    "serviette": "NA",
}

SAFE_VESSEL_HINTS = [
    ("kaffeebecher", "CU"),
    ("suppenbecher", "CU"),
    ("becher", "CU"),
    ("gobelet", "CU"),
    ("cup", "CU"),
    ("schale", "BM"),
    ("bowl", "BM"),
    ("teller", "PU"),
    ("platte", "PU"),
    ("plate", "PU"),
    ("flasche", "BO"),
    ("bottle", "BO"),
    ("glas", "GR"),
    ("glass", "GR"),
    ("jar", "JR"),
    ("kanister", "CI"),
    ("canister", "CI"),
    ("container", "WA"),
    ("deckel", "NA"),
    ("lid", "NA"),
    ("menübox", "BX"),
    ("menuebox", "BX"),
    ("box", "BX"),
    ("serviette", "NA"),
    ("napkin", "NA"),
    ("trinkhalm", "NA"),
    ("straw", "NA"),
    ("beutel", "BG"),
    ("tragetasche", "BG"),
    ("bag", "BG"),
]

UNIT_SYNONYMS = {
    "liter": "l",
    "litre": "l",
    "l": "l",
    "dl": "dl",
    "cl": "cl",
    "ml": "ml",
    "kilogram": "kg",
    "kilo": "kg",
    "kg": "kg",
    "gram": "g",
    "gramm": "g",
    "g": "g",
    "pcs": "quantity",
    "pc": "quantity",
    "piece": "quantity",
    "pieces": "quantity",
    "stück": "quantity",
    "stk": "quantity",
    "quantity": "quantity",
}


def normalize_empty(value: str, *, allow_na_code: bool = False) -> str:
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered in EMPTY_SENTINELS:
        return ""
    if lowered == "na" and not allow_na_code:
        return ""
    return stripped


def normalize_decimal_string(value: str) -> str:
    normalized = value.replace("'", "").replace(" ", "").replace(",", ".")
    if normalized.endswith(".0"):
        normalized = normalized[:-2]
    return normalized


def normalize_url(value: str) -> str:
    value = normalize_empty(value)
    if not value:
        return ""
    return value if re.match(r"^https?://", value) else ""


def record_change(
    report: list[dict[str, str]],
    *,
    row_number: int,
    item_id_before: str,
    item_id_after: str,
    field: str,
    value_before: str,
    value_after: str,
    reason: str,
) -> None:
    if value_before == value_after:
        return
    report.append(
        {
            "row_number": str(row_number),
            "item_id_before": item_id_before,
            "item_id_after": item_id_after,
            "field": field,
            "value_before": value_before,
            "value_after": value_after,
            "reason": reason,
        }
    )


def sanitize_item_id(value: str) -> str:
    cleaned = value.strip().replace("/", "-").replace("\\", "-")
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = ITEM_ID_ALLOWED_PATTERN.sub("-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.strip()


def normalize_type_code(value: str, *, allow_px: bool) -> str:
    value = normalize_empty(value, allow_na_code=True)
    if not value:
        return ""
    upper = value.upper()
    if TYPE_CODE_PATTERN.fullmatch(upper):
        if upper == "PX" and not allow_px:
            return ""
        return upper
    lowered = value.lower().strip()
    lowered = lowered.replace(".", "")
    for token in re.split(r"[\s/_,-]+", lowered):
        if token in TYPE_SYNONYMS:
            code = TYPE_SYNONYMS[token]
            if code == "PX" and not allow_px:
                return ""
            return code
    if lowered in TYPE_SYNONYMS:
        code = TYPE_SYNONYMS[lowered]
        if code == "PX" and not allow_px:
            return ""
        return code
    return ""


def infer_type_code_from_text(text: str, *, allow_px: bool) -> str:
    lowered = text.lower()
    for token, code in TYPE_SYNONYMS.items():
        if token in lowered:
            if code == "PX" and not allow_px:
                continue
            return code
    return ""


def infer_vessel_type_from_name(name: str) -> str:
    lowered = name.lower()
    for hint, code in SAFE_VESSEL_HINTS:
        if hint in lowered:
            return code
    return ""


def normalize_unit(value: str) -> str:
    value = normalize_empty(value)
    if not value:
        return ""
    lowered = value.lower().strip().rstrip(".")
    return UNIT_SYNONYMS.get(lowered, "")


def parse_price(value: str) -> str:
    stripped = normalize_empty(value)
    if not stripped:
        return ""
    stripped = re.sub(r"(?i)chf|eur|usd|sfr|€|\$", "", stripped)
    match = PRICE_EXTRACT_PATTERN.search(stripped)
    if not match:
        return ""
    number = normalize_decimal_string(match.group(0))
    try:
        return f"{float(number):.2f}"
    except ValueError:
        return ""


def parse_vat(value: str) -> str:
    stripped = normalize_empty(value)
    if not stripped:
        return ""
    numeric = normalize_decimal_string(stripped)
    if numeric in ALLOWED_VAT_VALUES:
        return numeric
    return ""


def normalize_labels(value: str) -> str:
    stripped = normalize_empty(value)
    if not stripped:
        return ""
    labels = []
    for label in stripped.split(","):
        normalized = label.strip().upper()
        if normalized in ALLOWED_LABELS and normalized not in labels:
            labels.append(normalized)
    return ",".join(labels)


def parse_numeric_field(value: str) -> str:
    stripped = normalize_empty(value)
    if not stripped:
        return ""
    match = PRICE_EXTRACT_PATTERN.search(stripped)
    if not match:
        return ""
    return normalize_decimal_string(match.group(0))


def parse_float_value(value: str) -> float | None:
    stripped = normalize_empty(value)
    if not stripped:
        return None
    try:
        return float(normalize_decimal_string(stripped))
    except ValueError:
        return None


def is_integer_numeric_string(value: str) -> bool:
    number = parse_float_value(value)
    return number is not None and number.is_integer()


def round_to_nearest_integer(value: str) -> str:
    try:
        number = Decimal(normalize_decimal_string(value))
    except (InvalidOperation, ValueError):
        return value
    return str(int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def format_decimal(number: Decimal) -> str:
    return format(number.normalize(), "f")


def round_vessel_size_to_unit_precision(value: str, unit: str) -> str:
    """Round positive vessel values to YourBarMate's supported unit precision."""
    decimal_places = VESSEL_DECIMAL_PLACES.get(unit)
    if decimal_places is None:
        return value
    try:
        number = Decimal(normalize_decimal_string(value))
    except (InvalidOperation, ValueError):
        return value
    if number <= 0:
        return value
    smallest_supported_value = Decimal(1).scaleb(-decimal_places)
    rounded = number.quantize(smallest_supported_value, rounding=ROUND_HALF_UP)
    # A positive physical vessel cannot be exported as zero after rounding.
    if rounded == 0:
        rounded = smallest_supported_value
    return format_decimal(rounded)


def is_valid_bundle_size(value: str) -> bool:
    number = parse_float_value(value)
    return number is not None and number.is_integer() and int(number) >= 2


def maybe_parse_multipack(text: str) -> tuple[str, str, str]:
    match = MULTIPACK_PATTERN.search(text)
    if not match:
        return "", "", ""
    bundle_size = normalize_decimal_string(match.group("count"))
    vessel_size = normalize_decimal_string(match.group("size"))
    unit = normalize_unit(match.group("unit"))
    return bundle_size, vessel_size, unit


def multiply_numeric_strings(left: str, right: str) -> str:
    try:
        result = float(left) * float(right)
    except ValueError:
        return left
    if result.is_integer():
        return str(int(result))
    return f"{result:g}"


def maybe_parse_single_size(text: str) -> tuple[str, str]:
    match = SINGLE_SIZE_PATTERN.search(text)
    if not match:
        return "", ""
    return normalize_decimal_string(match.group("size")), normalize_unit(match.group("unit"))


def maybe_parse_bundle_count(text: str) -> tuple[str, str]:
    match = BUNDLE_COUNT_PATTERN.search(text)
    if not match:
        return "", ""
    count = normalize_decimal_string(match.group("count"))
    bundle_type = normalize_type_code(match.group("type"), allow_px=True)
    return count, bundle_type


def clean_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    cleaned_rows = [deepcopy(row) for row in rows]
    corrections: list[dict[str, str]] = []

    original_item_ids = [row.get("Item ID", "") for row in cleaned_rows]
    sanitized_ids: list[str] = []
    for index, row in enumerate(cleaned_rows, start=2):
        before = row["Item ID"]
        after = sanitize_item_id(before)
        if not after:
            after = f"row{index:05d}"
        sanitized_ids.append(after)
        row["Item ID"] = after
        record_change(
            corrections,
            row_number=index,
            item_id_before=before,
            item_id_after=after,
            field="Item ID",
            value_before=before,
            value_after=after,
            reason="sanitize_item_id",
        )

    seen: dict[str, int] = {}
    for index, row in enumerate(cleaned_rows, start=2):
        current = row["Item ID"]
        if current in seen:
            deduped = f"{current}-row{index:05d}"
            record_change(
                corrections,
                row_number=index,
                item_id_before=original_item_ids[index - 2],
                item_id_after=deduped,
                field="Item ID",
                value_before=current,
                value_after=deduped,
                reason="deduplicate_sanitized_item_id",
            )
            row["Item ID"] = deduped
            current = deduped
        seen[current] = index

    for index, row in enumerate(cleaned_rows, start=2):
        original_item_id = original_item_ids[index - 2]
        final_item_id = row["Item ID"]
        item_text = " ".join(
            filter(
                None,
                [
                    row.get("Item name", ""),
                    row.get("Description", ""),
                    row.get("Category name", ""),
                    row.get("Material", ""),
                ],
            )
        )

        for field in CSV_COLUMNS:
            before = row.get(field, "")
            allow_na_code = field in {"Vessel type", "Bundle type"}
            after = normalize_empty(before, allow_na_code=allow_na_code)
            row[field] = after
            record_change(
                corrections,
                row_number=index,
                item_id_before=original_item_id,
                item_id_after=final_item_id,
                field=field,
                value_before=before,
                value_after=after,
                reason="normalize_empty_value",
            )

        category_before = row["Category name"]
        if not row["Category name"]:
            row["Category name"] = "Uncategorized"
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Category name", value_before=category_before, value_after=row["Category name"], reason="default_uncategorized")

        for field, allowed, default_value in [
            ("Order by", ALLOWED_ORDER_BY, "vessel"),
            ("Price per", ALLOWED_PRICE_PER, "vessel"),
            ("Status", ALLOWED_STATUS, "ACTIVE"),
        ]:
            before = row[field]
            after = before if before in allowed else default_value
            row[field] = after
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field=field, value_before=before, value_after=after, reason="normalize_enum")

        before_price = row["Price"]
        row["Price"] = parse_price(before_price)
        record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Price", value_before=before_price, value_after=row["Price"], reason="normalize_price")

        before_vat = row["VAT"]
        row["VAT"] = parse_vat(before_vat)
        record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="VAT", value_before=before_vat, value_after=row["VAT"], reason="normalize_vat")

        before_labels = row["Labels"]
        row["Labels"] = normalize_labels(before_labels)
        record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Labels", value_before=before_labels, value_after=row["Labels"], reason="normalize_labels")

        for field in ["Image", "Product Sheet"]:
            before = row[field]
            row[field] = normalize_url(before)
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field=field, value_before=before, value_after=row[field], reason="normalize_url")

        for field in ["Vessel size", "Bundle size", "Length", "Width", "Height", "Diameter", "Net weight", "Total weight"]:
            before = row[field]
            after = parse_numeric_field(before)
            row[field] = after
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field=field, value_before=before, value_after=after, reason="normalize_numeric_field")

        multipack_bundle_size, multipack_vessel_size, multipack_unit = maybe_parse_multipack(item_text)
        bundle_count_from_text, bundle_type_from_text = maybe_parse_bundle_count(item_text)
        single_size_from_text, single_unit_from_text = maybe_parse_single_size(item_text)

        if multipack_bundle_size and multipack_vessel_size and multipack_unit == "quantity":
            multipack_bundle_size = multiply_numeric_strings(multipack_bundle_size, multipack_vessel_size)
            multipack_vessel_size = "1"

        before_unit = row["Vessel unit"]
        normalized_unit = normalize_unit(row["Vessel unit"])
        if not normalized_unit and multipack_unit:
            normalized_unit = multipack_unit
        if not normalized_unit and single_unit_from_text:
            normalized_unit = single_unit_from_text
        if not normalized_unit:
            normalized_unit = "quantity"
        row["Vessel unit"] = normalized_unit
        record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Vessel unit", value_before=before_unit, value_after=normalized_unit, reason="normalize_vessel_unit")

        before_vessel_size = row["Vessel size"]
        vessel_size = row["Vessel size"]
        if not vessel_size and multipack_vessel_size:
            vessel_size = multipack_vessel_size
        if not vessel_size and single_size_from_text and normalized_unit != "quantity":
            vessel_size = single_size_from_text
        if not vessel_size and normalized_unit == "quantity":
            vessel_size = "1"
        row["Vessel size"] = vessel_size
        record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Vessel size", value_before=before_vessel_size, value_after=vessel_size, reason="normalize_vessel_size")

        if row["Vessel unit"] in VESSEL_DECIMAL_PLACES and row["Vessel size"]:
            before_size = row["Vessel size"]
            rounded_size = round_vessel_size_to_unit_precision(before_size, row["Vessel unit"])
            if rounded_size != before_size:
                row["Vessel size"] = rounded_size
                reason = f"round_vessel_size_{row['Vessel unit']}"
                record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Vessel size", value_before=before_size, value_after=rounded_size, reason=reason)

        interpreted_flattened_quantity = (
            row["Vessel unit"] == "quantity"
            and bool(row["Vessel size"])
            and row["Vessel size"] not in {"", "1"}
            and not row["Bundle size"]
        )

        before_vessel_type = row["Vessel type"]
        vessel_type = normalize_type_code(row["Vessel type"], allow_px=False)
        if not vessel_type:
            vessel_type = infer_vessel_type_from_name(row["Item name"])
        row["Vessel type"] = vessel_type
        record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Vessel type", value_before=before_vessel_type, value_after=vessel_type, reason="normalize_vessel_type")

        before_bundle_size = row["Bundle size"]
        bundle_size = row["Bundle size"]
        if not bundle_size and multipack_bundle_size and not interpreted_flattened_quantity:
            bundle_size = multipack_bundle_size
        if not bundle_size and bundle_count_from_text and not interpreted_flattened_quantity:
            bundle_size = bundle_count_from_text
        row["Bundle size"] = bundle_size
        record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Bundle size", value_before=before_bundle_size, value_after=bundle_size, reason="normalize_bundle_size")

        before_bundle_type = row["Bundle type"]
        bundle_type = normalize_type_code(row["Bundle type"], allow_px=True)
        if not bundle_type and bundle_type_from_text:
            bundle_type = bundle_type_from_text
        if not bundle_type and row["Bundle size"] and row["Bundle size"] != "1":
            bundle_type = "PK"
        if not row["Bundle size"] or row["Bundle size"] == "1":
            bundle_type = ""
            if row["Bundle size"] == "1":
                row["Bundle size"] = ""
        row["Bundle type"] = bundle_type
        record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Bundle type", value_before=before_bundle_type, value_after=bundle_type, reason="normalize_bundle_type")
        if before_bundle_size != row["Bundle size"]:
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Bundle size", value_before=before_bundle_size, value_after=row["Bundle size"], reason="clear_bundle_size_one_or_invalid")

        if row["Bundle size"] and row["Bundle size"] != "1" and not row["Bundle type"]:
            before = row["Bundle type"]
            row["Bundle type"] = "PK"
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Bundle type", value_before=before, value_after="PK", reason="fallback_bundle_type")

        if row["Bundle size"] and not is_valid_bundle_size(row["Bundle size"]):
            before_size = row["Bundle size"]
            before_type = row["Bundle type"]
            row["Bundle size"] = ""
            row["Bundle type"] = ""
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Bundle size", value_before=before_size, value_after="", reason="invalid_bundle_size")
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Bundle type", value_before=before_type, value_after="", reason="clear_bundle_type_for_invalid_bundle_size")

        if row["Bundle GTIN"] and row["GTIN"] and row["Bundle GTIN"] == row["GTIN"]:
            before = row["Bundle GTIN"]
            row["Bundle GTIN"] = ""
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Bundle GTIN", value_before=before, value_after="", reason="clear_duplicate_bundle_gtin")

        for field in ["GTIN", "Bundle GTIN"]:
            before = row[field]
            after = re.sub(r"\D", "", before)
            if after and not 8 <= len(after) <= 14:
                after = ""
            row[field] = after
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field=field, value_before=before, value_after=after, reason="normalize_gtin")

        # Final enum guardrails.
        if row["Vessel unit"] not in ALLOWED_VESSEL_UNITS:
            before = row["Vessel unit"]
            row["Vessel unit"] = "quantity"
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Vessel unit", value_before=before, value_after="quantity", reason="fallback_vessel_unit")
        if row["Vessel type"] and row["Vessel type"] not in ALLOWED_VESSEL_TYPES:
            before = row["Vessel type"]
            row["Vessel type"] = ""
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Vessel type", value_before=before, value_after="", reason="invalid_vessel_type")
        if row["Bundle type"] and row["Bundle type"] not in ALLOWED_BUNDLE_TYPES:
            before = row["Bundle type"]
            row["Bundle type"] = "PK" if row["Bundle size"] else ""
            record_change(corrections, row_number=index, item_id_before=original_item_id, item_id_after=final_item_id, field="Bundle type", value_before=before, value_after=row["Bundle type"], reason="invalid_bundle_type")

    return cleaned_rows, corrections


def clean_csv_file(input_path: Path, output_path: Path, report_path: Path) -> tuple[int, int]:
    with input_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    cleaned_rows, corrections = clean_rows(rows)
    write_csv_rows(cleaned_rows, output_path)
    write_correction_report(corrections, report_path)
    return len(cleaned_rows), len(corrections)


def write_correction_report(corrections: list[dict[str, str]], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CORRECTION_REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(corrections)
