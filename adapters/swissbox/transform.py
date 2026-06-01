from __future__ import annotations

from models import NormalizedProduct
from parse import parse_product_page, product_candidate_from_url


def parse_product_record(html: str, url: str) -> NormalizedProduct | None:
    return parse_product_page(html, url)


__all__ = ["parse_product_record", "product_candidate_from_url"]
