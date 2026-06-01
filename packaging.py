from __future__ import annotations

import csv
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from models import NormalizedProduct


PACKAGING_AUDIT_COLUMNS = [
    "Item ID",
    "Item name",
    "Old vessel size",
    "Old vessel unit",
    "Old vessel type",
    "Old bundle size",
    "Old bundle type",
    "Old price",
    "New vessel size",
    "New vessel unit",
    "New vessel type",
    "New bundle size",
    "New bundle type",
    "New price",
    "Interpretation mode",
    "Evidence text",
    "Parser source used",
]

MEASURE_RE = re.compile(r"(?P<value>\d[\d'.,]*)\s*(?P<unit>ml|cl|dl|l|liter|litre|kg|kilo|g|gramm)\b", re.IGNORECASE)
FILL_RE = re.compile(
    r"(?P<value>\d[\d'.,]*)\s*(?P<unit>ml|cl|dl|l|liter|litre|kg|kilo|g|gramm)\s*/\s*(?P<named_unit>spender-?flasche|schwedenflasche|flasche|rolle|beutel|dose|kanister)\b",
    re.IGNORECASE,
)
OUTER_CONTAINER_RE = r"karton|pack|box|palette|pallet|umsack|beutel|rolle"
PIECE_TOKEN_RE = r"stück|stueck|stk\.?|pieces?|pcs?|blätter|blaetter|blatt"
NAMED_UNIT_RE = r"einzelflaschen?|einzelrollen?|einzelbeutel|einzeldosen?|einzelkanister|einzelsack|spender-?flaschen?|schwedenflaschen?|flaschen?|rollen?|beutel|dosen?|kanister|packs?|boxen?|sack|säcke|saecke"
SIMPLE_OUTER_RE = re.compile(
    rf"(?P<count>\d[\d'.,]*)\s*(?P<unit>{NAMED_UNIT_RE}|{PIECE_TOKEN_RE})\s*(?:/\s*(?P<container>{OUTER_CONTAINER_RE}))?",
    re.IGNORECASE,
)
NESTED_COUNT_RE = re.compile(
    rf"(?P<outer>\d[\d'.,]*)\s*[xX]\s*(?P<inner>\d[\d'.,]*)\s*(?P<inner_unit>{PIECE_TOKEN_RE}|rollen?|beutel|säcke|saecke)\s*(?:/\s*(?P<container>{OUTER_CONTAINER_RE}))?",
    re.IGNORECASE,
)
NESTED_NAMED_RE = re.compile(
    rf"(?P<outer>\d[\d'.,]*)\s*(?P<outer_unit>{NAMED_UNIT_RE})\s*(?:à|[xX])\s*(?P<inner>\d[\d'.,]*)\s*(?P<inner_unit>{PIECE_TOKEN_RE}|beutel|säcke|saecke|rollen?)\s*(?:/\s*(?P<container>{OUTER_CONTAINER_RE}))?",
    re.IGNORECASE,
)
PHYSICAL_OUTER_RE = re.compile(
    rf"(?P<outer>\d[\d'.,]*)\s*[xX]\s*(?P<size>\d[\d'.,]*)\s*(?P<size_unit>ml|cl|dl|l|liter|litre|kg|kilo|g|gramm)\s*(?P<named_unit>spender-?flasche|schwedenflasche|flasche|beutel|dose|kanister)\s*(?:/\s*(?P<container>{OUTER_CONTAINER_RE}))?",
    re.IGNORECASE,
)
PHYSICAL_COUNT_ONLY_RE = re.compile(
    rf"(?P<outer>\d[\d'.,]*)\s*[xX]\s*(?P<size>\d[\d'.,]*)\s*(?P<size_unit>ml|cl|dl|l|liter|litre|kg|kilo|g|gramm)\s*(?:/\s*(?P<container>{OUTER_CONTAINER_RE}))?",
    re.IGNORECASE,
)
LEADING_NAMED_UNIT_RE = re.compile(
    rf"(?P<vessel_named>rolle|pack|box|beutel)\s*(?:à|[xX])\s*(?P<inner>\d[\d'.,]*)\s*(?P<inner_unit>{PIECE_TOKEN_RE}|beutel|säcke|saecke|rollen?)\s*,\s*(?P<outer>\d[\d'.,]*)\s*(?P<outer_unit>{NAMED_UNIT_RE})\s*(?:/\s*(?P<container>{OUTER_CONTAINER_RE}))?",
    re.IGNORECASE,
)

PRESERVE_NAMED_UNITS = {
    "einzelflasche": "bottle",
    "einzelrolle": "roll",
    "einzelbeutel": "bag",
    "einzeldose": "can",
    "einzelkanister": "canister",
    "einzelsack": "bag",
    "spender-flasche": "bottle",
    "spenderflasche": "bottle",
    "schwedenflasche": "bottle",
    "flasche": "bottle",
    "flaschen": "bottle",
    "rolle": "roll",
    "rollen": "roll",
    "beutel": "bag",
    "dose": "can",
    "dosen": "can",
    "kanister": "canister",
    "pack": "pack",
    "packs": "pack",
    "box": "box",
    "boxen": "box",
    "sack": "bag",
    "säcke": "bag",
    "saecke": "bag",
}

PIECE_UNITS = {
    "stück",
    "stueck",
    "stk",
    "stk.",
    "piece",
    "pieces",
    "pcs",
    "pcs.",
    "blatt",
    "blätter",
    "blaetter",
}

CONTAINER_WORDS = {
    "karton": "Karton",
    "pack": "Pack",
    "box": "Box",
    "palette": "Palette",
    "pallet": "Palette",
    "umsack": "Pack",
    "beutel": "Beutel",
    "rolle": "Rolle",
}


@dataclass
class PackagingDecision:
    vessel_size: str
    vessel_unit: str
    vessel_type: str
    bundle_size: str
    bundle_type: str
    price: str
    mode: str
    evidence: str
    source: str
    issue: str = ""


def normalize_number(value: str) -> str:
    cleaned = value.replace("'", "").replace("’", "").replace(" ", "").replace(",", ".").strip()
    if cleaned.endswith(".0"):
        cleaned = cleaned[:-2]
    return cleaned


def as_decimal(value: str) -> Decimal | None:
    cleaned = normalize_number(value)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def format_decimal(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{quantized:.2f}"


def normalize_measure_unit(unit: str) -> str:
    lowered = unit.lower()
    if lowered in {"liter", "litre", "l"}:
        return "l"
    if lowered in {"kilo", "kg"}:
        return "kg"
    if lowered in {"gramm", "g"}:
        return "g"
    return lowered


def singularize(token: str) -> str:
    lowered = token.strip().lower().replace("ä", "ae")
    lowered = lowered.replace("ö", "oe").replace("ü", "ue")
    if lowered.endswith("."):
        lowered = lowered[:-1]
    if lowered in {"flaschen", "flasche"}:
        return "flasche"
    if lowered in {"einzelflaschen", "einzelflasche"}:
        return "einzelflasche"
    if lowered in {"rollen", "rolle"}:
        return "rolle"
    if lowered in {"einzelrollen", "einzelrolle"}:
        return "einzelrolle"
    if lowered in {"dosen", "dose"}:
        return "dose"
    if lowered in {"einzeldosen", "einzeldose"}:
        return "einzeldose"
    if lowered in {"kanister"}:
        return "kanister"
    if lowered in {"einzelkanister"}:
        return "einzelkanister"
    if lowered in {"einzelbeutel"}:
        return "einzelbeutel"
    if lowered in {"packs", "pack"}:
        return "pack"
    if lowered in {"boxen", "box"}:
        return "box"
    if lowered in {"saecke", "säcke", "sack"}:
        return "sack"
    if lowered in {"einzelsack"}:
        return "einzelsack"
    if lowered in {"blätter", "blaetter", "blatt"}:
        return "blatt"
    if lowered.endswith("en") and lowered[:-2] in {"roll", "flasch", "dos", "box"}:
        return lowered[:-2] + ("e" if lowered[:-2] == "roll" else "")
    if lowered.endswith("n") and lowered[:-1] in {"flasche", "rolle", "dose", "box", "pack"}:
        return lowered[:-1]
    if lowered.endswith("e") and lowered[:-1] == "saeck":
        return "sack"
    return lowered


def title_container(token: str) -> str:
    if not token:
        return ""
    return CONTAINER_WORDS.get(token.strip().lower(), token.strip().title())


def collect_packaging_sources(product: NormalizedProduct) -> list[tuple[str, str]]:
    return [
        ("spec_stueckzahl", product.raw_spec_piece_text or product.specs.get("Stückzahl", "")),
        ("description", product.description),
        ("detail_price_unit", product.raw_detail_price_unit_text or product.raw_bundle_text),
        ("spec_fill", product.raw_fill_text or product.specs.get("Füllmenge", "") or product.specs.get("Inhalt", "")),
        ("item_name", product.item_name),
    ]


def first_matching_text(pattern: re.Pattern[str], sources: list[tuple[str, str]]) -> tuple[re.Match[str] | None, str, str]:
    for source_name, text in sources:
        if not text:
            continue
        match = pattern.search(text)
        if match:
            return match, source_name, text
    return None, "", ""


def decide_vessel_type_from_named_unit(named_unit: str) -> str:
    token = singularize(named_unit)
    return PRESERVE_NAMED_UNITS.get(token, PRESERVE_NAMED_UNITS.get(f"{token}n", ""))


def choose_fill_measure(product: NormalizedProduct, sources: list[tuple[str, str]]) -> tuple[str, str, str]:
    explicit_fill_text = product.raw_fill_text or product.specs.get("Füllmenge", "")
    if explicit_fill_text:
        fill_match = FILL_RE.search(explicit_fill_text)
        if fill_match:
            return (
                normalize_number(fill_match.group("value")),
                normalize_measure_unit(fill_match.group("unit")),
                decide_vessel_type_from_named_unit(fill_match.group("named_unit")),
            )
    non_detail_sources = [item for item in sources if item[0] != "detail_price_unit"]
    fill_match, _, _ = first_matching_text(FILL_RE, non_detail_sources)
    if fill_match:
        return (
            normalize_number(fill_match.group("value")),
            normalize_measure_unit(fill_match.group("unit")),
            decide_vessel_type_from_named_unit(fill_match.group("named_unit")),
        )
    fill_match, _, _ = first_matching_text(FILL_RE, [item for item in sources if item[0] == "detail_price_unit"])
    if fill_match:
        return (
            normalize_number(fill_match.group("value")),
            normalize_measure_unit(fill_match.group("unit")),
            decide_vessel_type_from_named_unit(fill_match.group("named_unit")),
        )
    for source_name, text in sources:
        if source_name == "detail_price_unit":
            continue
        if not text:
            continue
        measure = MEASURE_RE.search(text)
        if measure:
            return normalize_number(measure.group("value")), normalize_measure_unit(measure.group("unit")), ""
    return "", "", ""


def context_named_vessel_type(product: NormalizedProduct) -> str:
    haystack = " ".join(
        filter(
            None,
            [
                product.item_name.lower(),
                product.product_name.lower(),
                (product.raw_fill_text or product.specs.get("Füllmenge", "")).lower(),
                (product.raw_spec_piece_text or product.specs.get("Stückzahl", "")).lower(),
            ],
        )
    )
    for needle, vessel_type in [
        ("bag-in-box", "bag"),
        ("rolle", "roll"),
        ("rollen", "roll"),
        ("flasche", "bottle"),
        ("schwedenflasche", "bottle"),
        ("spender-flasche", "bottle"),
        ("beutel", "bag"),
        ("kanister", "canister"),
        ("dose", "can"),
        ("dosen", "can"),
    ]:
        if needle in haystack:
            return vessel_type
    return ""


def is_single_piece(text: str) -> bool:
    normalized = text.strip().lower()
    return bool(re.search(r"\b1\s*(stück|stueck|stk\.?)\b", normalized))


def descriptive_capacity_piece_vessel_type(product: NormalizedProduct) -> str:
    haystack = " ".join(
        filter(
            None,
            [
                product.item_name.lower(),
                product.product_name.lower(),
                product.category_path.lower(),
                (product.raw_spec_piece_text or product.specs.get("Stückzahl", "") or product.specs.get("Anzahl", "")).lower(),
            ],
        )
    )
    if "deckel" in haystack:
        return "lid"
    if "gn-behälter" in haystack or "gn-behaelter" in haystack:
        return "container"
    if "eimer" in haystack or "bucket" in haystack:
        return "bucket"
    return ""


def should_use_descriptive_capacity_as_name_only(product: NormalizedProduct) -> bool:
    piece_text = product.raw_spec_piece_text or product.specs.get("Stückzahl", "") or product.specs.get("Anzahl", "")
    if not piece_text or not is_single_piece(piece_text):
        return False
    if descriptive_capacity_piece_vessel_type(product):
        return True
    return False


def has_named_unit_context(product: NormalizedProduct, token: str) -> bool:
    haystack = " ".join(
        filter(
            None,
            [
                product.item_name.lower(),
                product.product_name.lower(),
                (product.raw_fill_text or "").lower(),
                (product.raw_spec_piece_text or product.specs.get("Stückzahl", "")).lower(),
            ],
        )
    )
    singular = singularize(token)
    return singular in haystack or token.lower() in haystack


def divide_price(price: str, divisor: str) -> str:
    price_value = as_decimal(price)
    divisor_value = as_decimal(divisor)
    if price_value is None or divisor_value is None or divisor_value == 0:
        return price
    return format_decimal(price_value / divisor_value)


def already_interpreted_division(
    product: NormalizedProduct,
    *,
    bundle_size: str,
    vessel_size: str,
    vessel_unit: str,
    vessel_type: str,
) -> bool:
    if product.packaging_mode != "preserve_inner_unit_divide_price":
        return False
    if normalize_number(product.bundle_size or "") != normalize_number(bundle_size):
        return False
    if (product.vessel_size or "") != (vessel_size or ""):
        return False
    if (product.vessel_unit or "") != (vessel_unit or ""):
        return False
    if vessel_type and (product.vessel_type or "") != vessel_type:
        return False
    return True


def multiply_strings(left: str, right: str) -> str:
    left_value = as_decimal(left)
    right_value = as_decimal(right)
    if left_value is None or right_value is None:
        return ""
    result = left_value * right_value
    if result == int(result):
        return str(int(result))
    return normalize_number(f"{result}")


def build_decision(
    product: NormalizedProduct,
    *,
    vessel_size: str,
    vessel_unit: str,
    vessel_type: str,
    bundle_size: str,
    bundle_type: str,
    price: str,
    mode: str,
    evidence: str,
    source: str,
    issue: str = "",
) -> PackagingDecision:
    return PackagingDecision(
        vessel_size=vessel_size or product.vessel_size,
        vessel_unit=vessel_unit or product.vessel_unit,
        vessel_type=vessel_type or product.vessel_type,
        bundle_size=bundle_size,
        bundle_type=bundle_type if bundle_size else "",
        price=price or product.price,
        mode=mode,
        evidence=evidence,
        source=source,
        issue=issue,
    )


def interpret_packaging(product: NormalizedProduct) -> PackagingDecision:
    sources = collect_packaging_sources(product)
    piece_match, piece_source, piece_text = first_matching_text(NESTED_COUNT_RE, sources)
    named_nested_match, named_nested_source, named_nested_text = first_matching_text(NESTED_NAMED_RE, sources)
    physical_match, physical_source, physical_text = first_matching_text(PHYSICAL_OUTER_RE, sources)
    physical_count_only_match, physical_count_only_source, physical_count_only_text = first_matching_text(PHYSICAL_COUNT_ONLY_RE, sources)
    leading_named_match, leading_named_source, leading_named_text = first_matching_text(LEADING_NAMED_UNIT_RE, sources)
    simple_match, simple_source, simple_text = first_matching_text(SIMPLE_OUTER_RE, sources)
    fill_size, fill_unit, fill_vessel_type = choose_fill_measure(product, sources)
    detail_simple = SIMPLE_OUTER_RE.search(product.raw_detail_price_unit_text or product.raw_bundle_text)
    spec_simple = SIMPLE_OUTER_RE.search(
        product.raw_spec_piece_text or product.specs.get("Stückzahl", "") or product.description
    )

    if detail_simple and spec_simple:
        detail_unit = singularize(detail_simple.group("unit"))
        spec_unit = singularize(spec_simple.group("unit"))
        detail_count = normalize_number(detail_simple.group("count"))
        spec_count = normalize_number(spec_simple.group("count"))
        if (
            detail_unit in {"pack", "box"}
            and spec_unit in PIECE_UNITS
            and detail_count == spec_count
            and spec_count not in {"", "1"}
            and not has_named_unit_context(product, detail_unit)
        ):
            return build_decision(
                product,
                vessel_size=spec_count,
                vessel_unit="quantity",
                vessel_type=product.vessel_type,
                bundle_size="",
                bundle_type="",
                price=product.price,
                mode="flatten_piece_pack",
                evidence=product.raw_spec_piece_text or product.specs.get("Stückzahl", ""),
                source="spec_stueckzahl",
            )
        if (
            detail_unit in PRESERVE_NAMED_UNITS
            and spec_unit in PIECE_UNITS
            and normalize_number(spec_simple.group("count")) == "1"
            and not has_named_unit_context(product, detail_unit)
        ):
            return build_decision(
                product,
                vessel_size=product.vessel_size,
                vessel_unit=product.vessel_unit,
                vessel_type=product.vessel_type,
                bundle_size=product.bundle_size,
                bundle_type=product.bundle_type,
                price=product.price,
                mode="suspected_contaminated_bundle",
                evidence=product.raw_detail_price_unit_text or product.raw_bundle_text,
                source="detail_price_unit",
                issue="named unit in detail price block conflicts with product specs",
            )
        if (
            normalize_number(detail_simple.group("count")) == "1"
            and normalize_number(spec_simple.group("count")) not in {"", "1"}
            and fill_size
            and fill_unit
            and (fill_vessel_type or context_named_vessel_type(product)) in {"bag", "bottle", "can", "canister"}
        ):
            return build_decision(
                product,
                vessel_size=fill_size,
                vessel_unit=fill_unit,
                vessel_type=fill_vessel_type or context_named_vessel_type(product) or product.vessel_type,
                bundle_size="",
                bundle_type="",
                price=product.price,
                mode="preserve_single_named_unit",
                evidence=product.raw_detail_price_unit_text or product.raw_bundle_text,
                source="detail_price_unit",
            )

    if physical_match:
        outer = normalize_number(physical_match.group("outer"))
        vessel_size = normalize_number(physical_match.group("size"))
        vessel_unit = normalize_measure_unit(physical_match.group("size_unit"))
        vessel_type = decide_vessel_type_from_named_unit(physical_match.group("named_unit")) or fill_vessel_type or product.vessel_type
        bundle_type = title_container(physical_match.group("container") or "Karton")
        price = product.price
        if outer != "1" and not already_interpreted_division(
            product,
            bundle_size=outer,
            vessel_size=vessel_size,
            vessel_unit=vessel_unit,
            vessel_type=vessel_type,
        ):
            price = divide_price(product.price, outer)
        return build_decision(
            product,
            vessel_size=vessel_size,
            vessel_unit=vessel_unit,
            vessel_type=vessel_type,
            bundle_size=outer if outer != "1" else "",
            bundle_type=bundle_type,
            price=price,
            mode="preserve_inner_unit_divide_price" if outer != "1" else "",
            evidence=physical_text,
            source=physical_source,
        )

    if physical_count_only_match:
        outer = normalize_number(physical_count_only_match.group("outer"))
        vessel_size = normalize_number(physical_count_only_match.group("size"))
        vessel_unit = normalize_measure_unit(physical_count_only_match.group("size_unit"))
        vessel_type = fill_vessel_type or context_named_vessel_type(product) or product.vessel_type
        price = product.price
        if outer != "1" and not already_interpreted_division(
            product,
            bundle_size=outer,
            vessel_size=vessel_size,
            vessel_unit=vessel_unit,
            vessel_type=vessel_type,
        ):
            price = divide_price(product.price, outer)
        return build_decision(
            product,
            vessel_size=vessel_size,
            vessel_unit=vessel_unit,
            vessel_type=vessel_type,
            bundle_size=outer if outer != "1" else "",
            bundle_type=title_container(physical_count_only_match.group("container") or product.bundle_type or "Karton"),
            price=price,
            mode="preserve_inner_unit_divide_price" if outer != "1" else "",
            evidence=physical_count_only_text,
            source=physical_count_only_source,
        )

    if named_nested_match:
        outer = normalize_number(named_nested_match.group("outer"))
        inner = normalize_number(named_nested_match.group("inner"))
        outer_unit = named_nested_match.group("outer_unit")
        inner_unit = singularize(named_nested_match.group("inner_unit"))
        vessel_type = decide_vessel_type_from_named_unit(outer_unit)
        if inner_unit in PIECE_UNITS:
            vessel_size = inner
            vessel_unit = "quantity"
        else:
            vessel_size = inner
            vessel_unit = "quantity"
        price = product.price
        if outer != "1" and not already_interpreted_division(
            product,
            bundle_size=outer,
            vessel_size=vessel_size,
            vessel_unit=vessel_unit,
            vessel_type=vessel_type,
        ):
            price = divide_price(product.price, outer)
        return build_decision(
            product,
            vessel_size=vessel_size,
            vessel_unit=vessel_unit,
            vessel_type=vessel_type,
            bundle_size=outer if outer != "1" else "",
            bundle_type=title_container(named_nested_match.group("container") or product.bundle_type or "Karton"),
            price=price,
            mode="preserve_inner_unit_divide_price" if outer != "1" else "",
            evidence=named_nested_text,
            source=named_nested_source,
        )

    if leading_named_match:
        outer = normalize_number(leading_named_match.group("outer"))
        inner = normalize_number(leading_named_match.group("inner"))
        vessel_type = decide_vessel_type_from_named_unit(leading_named_match.group("vessel_named")) or context_named_vessel_type(product)
        price = product.price
        if outer != "1" and not already_interpreted_division(
            product,
            bundle_size=outer,
            vessel_size=inner,
            vessel_unit="quantity",
            vessel_type=vessel_type,
        ):
            price = divide_price(product.price, outer)
        return build_decision(
            product,
            vessel_size=inner,
            vessel_unit="quantity",
            vessel_type=vessel_type,
            bundle_size=outer if outer != "1" else "",
            bundle_type=title_container(leading_named_match.group("container") or product.bundle_type or "Karton"),
            price=price,
            mode="preserve_inner_unit_divide_price" if outer != "1" else "flatten_piece_pack",
            evidence=leading_named_text,
            source=leading_named_source,
        )

    if piece_match:
        outer = normalize_number(piece_match.group("outer"))
        inner = normalize_number(piece_match.group("inner"))
        effective = multiply_strings(outer, inner)
        return build_decision(
            product,
            vessel_size=effective,
            vessel_unit="quantity",
            vessel_type=product.vessel_type,
            bundle_size="",
            bundle_type="",
            price=product.price,
            mode="flatten_piece_pack",
            evidence=piece_text,
            source=piece_source,
        )

    if simple_match:
        count = normalize_number(simple_match.group("count"))
        unit_token = singularize(simple_match.group("unit"))
        container = title_container(simple_match.group("container") or "")

        if count == "1":
            if should_use_descriptive_capacity_as_name_only(product):
                return build_decision(
                    product,
                    vessel_size="1",
                    vessel_unit="quantity",
                    vessel_type=descriptive_capacity_piece_vessel_type(product) or product.vessel_type,
                    bundle_size="",
                    bundle_type="",
                    price=product.price,
                    mode="piece_unit_capacity_descriptive",
                    evidence=simple_text,
                    source=simple_source,
                )
            if fill_size and fill_unit:
                return build_decision(
                    product,
                    vessel_size=fill_size,
                    vessel_unit=fill_unit,
                    vessel_type=fill_vessel_type or context_named_vessel_type(product) or product.vessel_type,
                    bundle_size="",
                    bundle_type="",
                    price=product.price,
                    mode="preserve_single_named_unit",
                    evidence=simple_text,
                    source=simple_source,
                )
            return build_decision(
                product,
                vessel_size=product.vessel_size,
                vessel_unit=product.vessel_unit,
                vessel_type=product.vessel_type,
                bundle_size="",
                bundle_type="",
                price=product.price,
                mode="",
                evidence="",
                source="",
            )

        if unit_token in PIECE_UNITS:
            context_vessel_type = fill_vessel_type or context_named_vessel_type(product)
            if container in {"Rolle", "Beutel", "Box", "Pack"}:
                container_vessel_type = context_vessel_type or decide_vessel_type_from_named_unit(container)
                if container == "Rolle" and container_vessel_type in {"roll", "bag"}:
                    return build_decision(
                        product,
                        vessel_size=count,
                        vessel_unit="quantity",
                        vessel_type=container_vessel_type,
                        bundle_size="",
                        bundle_type="",
                        price=product.price,
                        mode="piece_count_in_named_unit",
                        evidence=simple_text,
                        source=simple_source,
                    )
                if container in {"Beutel", "Box", "Pack"} and context_vessel_type in {"roll", "bag", "box", "pack"}:
                    return build_decision(
                        product,
                        vessel_size=count,
                        vessel_unit="quantity",
                        vessel_type=container_vessel_type,
                        bundle_size="",
                        bundle_type="",
                        price=product.price,
                        mode="piece_count_in_named_unit",
                        evidence=simple_text,
                        source=simple_source,
                    )
            if context_vessel_type in {"bottle", "roll", "bag", "can", "canister"}:
                vessel_size = fill_size or product.vessel_size or "1"
                vessel_unit = fill_unit or product.vessel_unit or "quantity"
                if vessel_unit == "quantity" and not vessel_size:
                    vessel_size = "1"
                price = product.price
                if not already_interpreted_division(
                    product,
                    bundle_size=count,
                    vessel_size=vessel_size,
                    vessel_unit=vessel_unit,
                    vessel_type=context_vessel_type,
                ):
                    price = divide_price(product.price, count)
                return build_decision(
                    product,
                    vessel_size=vessel_size,
                    vessel_unit=vessel_unit,
                    vessel_type=context_vessel_type,
                    bundle_size=count,
                    bundle_type=container or title_container(product.bundle_type or "Pack"),
                    price=price,
                    mode="preserve_inner_unit_divide_price",
                    evidence=simple_text,
                    source=simple_source,
                )
            return build_decision(
                product,
                vessel_size=count,
                vessel_unit="quantity",
                vessel_type=product.vessel_type,
                bundle_size="",
                bundle_type="",
                price=product.price,
                mode="flatten_piece_pack",
                evidence=simple_text,
                source=simple_source,
            )

        if unit_token in PRESERVE_NAMED_UNITS:
            if not has_named_unit_context(product, unit_token):
                return build_decision(
                    product,
                    vessel_size=product.vessel_size,
                    vessel_unit=product.vessel_unit,
                    vessel_type=product.vessel_type,
                    bundle_size=product.bundle_size,
                    bundle_type=product.bundle_type,
                    price=product.price,
                    mode="suspected_contaminated_bundle",
                    evidence=simple_text,
                    source=simple_source,
                    issue="named unit in price block conflicts with product context",
                )
            if unit_token in {"pack", "box"} and not (fill_size and fill_unit):
                return build_decision(
                    product,
                    vessel_size=count,
                    vessel_unit="quantity",
                    vessel_type=product.vessel_type,
                    bundle_size="",
                    bundle_type="",
                    price=product.price,
                    mode="flatten_piece_pack",
                    evidence=simple_text,
                    source=simple_source,
                )
            vessel_type = decide_vessel_type_from_named_unit(unit_token) or fill_vessel_type or product.vessel_type
            vessel_size = fill_size or product.vessel_size or "1"
            vessel_unit = fill_unit or product.vessel_unit or "quantity"
            if vessel_unit == "quantity" and not vessel_size:
                vessel_size = "1"
            price = product.price
            if not already_interpreted_division(
                product,
                bundle_size=count,
                vessel_size=vessel_size,
                vessel_unit=vessel_unit,
                vessel_type=vessel_type,
            ):
                price = divide_price(product.price, count)
            return build_decision(
                product,
                vessel_size=vessel_size,
                vessel_unit=vessel_unit,
                vessel_type=vessel_type,
                bundle_size=count,
                bundle_type=container or title_container(product.bundle_type or "Karton"),
                price=price,
                mode="preserve_inner_unit_divide_price",
                evidence=simple_text,
                source=simple_source,
            )

    # Fallback: if we have a meaningful fill unit but no resolved bundle, preserve the single unit.
    if should_use_descriptive_capacity_as_name_only(product):
        return build_decision(
            product,
            vessel_size="1",
            vessel_unit="quantity",
            vessel_type=descriptive_capacity_piece_vessel_type(product) or product.vessel_type,
            bundle_size="",
            bundle_type="",
            price=product.price,
            mode="piece_unit_capacity_descriptive",
            evidence=product.raw_spec_piece_text or product.specs.get("Stückzahl", "") or product.specs.get("Anzahl", ""),
            source="spec_stueckzahl",
        )
    if fill_size and fill_unit and (product.vessel_unit == "quantity" or not product.vessel_unit):
        return build_decision(
            product,
            vessel_size=fill_size,
            vessel_unit=fill_unit,
            vessel_type=fill_vessel_type or context_named_vessel_type(product) or product.vessel_type,
            bundle_size=product.bundle_size,
            bundle_type=product.bundle_type,
            price=product.price,
            mode="preserve_single_named_unit",
            evidence=product.raw_fill_text or product.specs.get("Füllmenge", ""),
            source="spec_fill",
        )

    return build_decision(
        product,
        vessel_size=product.vessel_size,
        vessel_unit=product.vessel_unit,
        vessel_type=product.vessel_type,
        bundle_size=product.bundle_size,
        bundle_type=product.bundle_type,
        price=product.price,
        mode="",
        evidence="",
        source="",
    )


def apply_decision(product: NormalizedProduct, decision: PackagingDecision) -> None:
    product.vessel_size = decision.vessel_size
    product.vessel_unit = decision.vessel_unit
    product.vessel_type = decision.vessel_type
    product.bundle_size = decision.bundle_size
    product.bundle_type = decision.bundle_type
    product.price = decision.price
    product.packaging_mode = decision.mode or decision.issue
    product.packaging_evidence = decision.evidence
    product.packaging_source = decision.source


def build_audit_row(item_id: str, item_name: str, before: NormalizedProduct, after: NormalizedProduct) -> dict[str, str]:
    return {
        "Item ID": item_id,
        "Item name": item_name,
        "Old vessel size": before.vessel_size,
        "Old vessel unit": before.vessel_unit,
        "Old vessel type": before.vessel_type,
        "Old bundle size": before.bundle_size,
        "Old bundle type": before.bundle_type,
        "Old price": before.price,
        "New vessel size": after.vessel_size,
        "New vessel unit": after.vessel_unit,
        "New vessel type": after.vessel_type,
        "New bundle size": after.bundle_size,
        "New bundle type": after.bundle_type,
        "New price": after.price,
        "Interpretation mode": after.packaging_mode,
        "Evidence text": after.packaging_evidence,
        "Parser source used": after.packaging_source,
    }


def interpret_products(records: list[NormalizedProduct], item_ids: dict[str, str], record_keys: list[str]) -> list[dict[str, str]]:
    audit_rows: list[dict[str, str]] = []
    for record, record_key in zip(records, record_keys):
        before = deepcopy(record)
        decision = interpret_packaging(record)
        apply_decision(record, decision)
        if (
            before.vessel_size != record.vessel_size
            or before.vessel_unit != record.vessel_unit
            or before.vessel_type != record.vessel_type
            or before.bundle_size != record.bundle_size
            or before.bundle_type != record.bundle_type
            or before.price != record.price
            or record.packaging_mode
        ):
            audit_rows.append(build_audit_row(item_ids[record_key], record.item_name, before, record))
    return audit_rows


def write_packaging_audit(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PACKAGING_AUDIT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
