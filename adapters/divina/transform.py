from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from models import NormalizedProduct


PRODUCT_PATH = re.compile(r'/[^/]+/(?P<id>\d+)(?:-\d+)?-[^/]+\.html$')


def text(value) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r'/(\d+)-\d+-', r'/\1-', parts.path)
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def product_identifier(url: str) -> str:
    match = PRODUCT_PATH.search(urlsplit(url).path)
    return match.group('id') if match else ''


def extract_product_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, 'lxml')
    links = {}
    for link in soup.select('tr[data-reference] a.product-thumbnail[href], article.product-miniature a.product-thumbnail[href]'):
        url = canonical_url(urljoin(base_url, link['href']))
        if urlsplit(url).netloc == urlsplit(base_url).netloc and (identifier := product_identifier(url)):
            links[identifier] = url
    return sorted(links.values())


def extract_next_listing_url(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, 'lxml')
    link = soup.select_one('a.next[href], a[rel="next"][href]')
    return urljoin(base_url, link['href']) if link else ''


def extract_listing_total(html: str) -> int:
    soup = BeautifulSoup(html, 'lxml')
    match = re.search(r'von\s+([\d\s\x27’]+)\s+Artikel', soup.get_text(' ', strip=True))
    return int(re.sub(r'\D', '', match.group(1))) if match else 0


def _html_text(value) -> str:
    return BeautifulSoup(str(value or ''), 'lxml').get_text(' ', strip=True)


def packaging_from_title(name: str, sale_unit: str) -> dict[str, str]:
    """Preserve explicit supplier quantities, including cups containing mini pieces."""
    # Swiss thousands separators are part of the quantity, not a new measure.
    name = re.sub(r"(?<=\d)['’](?=\d{3}(?:\D|$))", '', name)
    measure = r'(?P<size>\d+(?:[.,]\d+)?)\s*(?P<unit>kg|g|gr|ml|cl|dl|l)\b'
    multiple = re.search(r'(?P<count>\d+)\s*[x×]\s*(?:ca\.?\s*)?' + measure, name, re.I)
    match = multiple or re.search(measure, name, re.I)
    if not match:
        return {}
    result = {'vessel_size': match['size'].replace(',', '.'), 'vessel_unit': match['unit'].lower().replace('gr', 'g')}
    explicit_outer = re.search(r'(\d+)\s*(?:Becher|Schalen|Gläser|Dosen)\s*/\s*(?:Krt|Karton)', name, re.I)
    trailing_outer = re.search(r'(?:Krt\.?|Karton)\s*(?:à|a)?\s*(\d+)\s*(?:Schalen|Sch\.|Becher|Stk)', name, re.I)
    if multiple and explicit_outer:
        result['vessel_size'] = str(Decimal(result['vessel_size']) * Decimal(multiple['count']))
        result.update(bundle_size=explicit_outer[1], bundle_type='CT')
    elif multiple:
        result.update(bundle_size=multiple['count'], bundle_type='CT' if 'Karton' in sale_unit else 'PK')
    elif trailing_outer:
        result.update(bundle_size=trailing_outer[1], bundle_type='CT')
    return result


def parse_product_record(html: str, url: str) -> NormalizedProduct | None:
    soup = BeautifulSoup(html, 'lxml')
    node = soup.select_one('[data-product]')
    if not node:
        return None
    payload = json.loads(node['data-product'])
    sku = text(payload.get('reference'))
    name = text(payload.get('name'))
    if not sku or not name:
        return None
    features: dict[str, str] = {}
    for feature in payload.get('features') or []:
        key, value = text(feature.get('name')), text(feature.get('value'))
        if key and value:
            features[key] = '; '.join(dict.fromkeys([features[key], value])) if key in features else value
    breadcrumbs = [n.get_text(' ', strip=True) for n in soup.select('.breadcrumb li')]
    category = ' > '.join(breadcrumbs[1:-1]) or text(payload.get('category_name'))
    cover = payload.get('cover') or {}
    image = ((cover.get('bySize') or {}).get('large_default') or {}).get('url') or cover.get('large', {}).get('url') or ''
    sheet_link = soup.select_one('.product-attachments a[href*="attachment"], .attachment a[href*="attachment"]')
    sheet = urljoin(url, sheet_link['href']) if sheet_link else ''
    description = [f'Lieferantenartikelnummer: {sku}', _html_text(payload.get('description')), _html_text(payload.get('description_short'))]
    description.extend(f'{key}: {value}' for key, value in features.items())
    if sheet:
        description.append(f'Spezifikation: {sheet}')
    # Anonymous stock/price presentation is gated by the shop login. Embedded
    # amounts and stock counters do not prove customer prices or orderability.
    specs = dict(features)
    specs['source_sku'] = sku
    specs['shop_product_id'] = text(payload.get('id_product') or payload.get('id'))
    specs['sale_attributes'] = json.dumps(payload.get('attributes') or {}, ensure_ascii=False)
    specs['source_availability'] = text(payload.get('availability_message'))
    gtin = ''
    for term in soup.select('dl.data-sheet dt'):
        if term.get_text(strip=True).lower() == 'ean13':
            value = term.find_next_sibling('dd')
            gtin = value.get_text(strip=True) if value else ''
    packaging = packaging_from_title(name, features.get('Einheit', ''))
    # The shop EAN belongs to the offered article; inner vs outer assignment is
    # not documented for bundles, so keep that barcode as source evidence.
    if packaging.get('bundle_size') and gtin:
        description.append(f'Artikel-GTIN laut Shop: {gtin}')
        specs['source_article_gtin'] = gtin
        gtin = ''
    allergens = features.get('Allergene', '')
    if not allergens:
        match = re.search(r'Allergene:\s*(?:Enthält\s*)?(.+)', features.get('Zusatzinfo', ''), re.I)
        allergens = match[1].strip() if match else ''
    return NormalizedProduct(
        product_url=canonical_url(url), canonical_url=canonical_url(url),
        category_path=category, product_name=name, item_name=name, sku=sku,
        image_url=urljoin(url, image) if image else '', gtin=gtin if gtin != '0000000000000' else '',
        currency='CHF', status='ACTIVE',
        min_order_count=text(payload.get('minimal_quantity')) or '1',
        brand=text(payload.get('manufacturer_name')),
        description='\n'.join(dict.fromkeys(part for part in description if part)),
        product_sheet_url=sheet,
        ingredients=features.get('Zutaten', ''), allergens=allergens,
        nutritional_values=features.get('Nährwerte', ''),
        region=features.get('Region', ''),
        alcohol_content=features.get('Volumen', '') if '%' in features.get('Volumen', '') else '',
        vintage=features.get('Jahrgang', '') if re.fullmatch(r'(?:19|20)\d{2}', features.get('Jahrgang', '')) else '',
        grape_variety=features.get('Traubensorte', '') if any(part in category.lower() for part in ('wein', 'prosecco', 'grappa', 'franciacorta')) else '',
        storage_advice=features.get('Lagerung', '') or features.get('Lagerhinweis', ''),
        country=features.get('Herkunft', '') or features.get('Ursprungsland', ''),
        raw_bundle_text=name, specs=specs,
        packaging_mode='structured_supplier_units' if packaging else '',
        packaging_source='divina_product_title' if packaging else '',
        packaging_evidence=name if packaging else '',
        **packaging,
    )
