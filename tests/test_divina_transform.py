from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from adapters.divina.transform import extract_product_links, extract_next_listing_url, extract_listing_total, parse_product_record
from adapters.divina.scraper import DivinaAdapter, robots_allows
from models import SupplierConfig
from export import build_csv_rows, record_key
from packaging import interpret_products
from cleaner import clean_rows

ROOT = Path(__file__).resolve().parents[1] / 'adapters/divina/fixtures'
BASE = 'https://shop.divinafood.ch'


class DivinaTransformTests(unittest.TestCase):
    def test_listing_discovers_products_and_pagination(self):
        html = (ROOT / 'listing.html').read_text()
        self.assertEqual(len(extract_product_links(html, BASE)), 4)
        self.assertEqual(extract_next_listing_url(html, BASE), BASE + '/2-alle-produkte?page=2')
        self.assertEqual(extract_listing_total(html), 856)
        self.assertEqual(extract_product_links('<html>No products</html>', BASE), [])

    def pipeline(self, index):
        product = parse_product_record((ROOT / f'detail{index}.html').read_text(), BASE + '/category/10007979-example.html')
        interpret_products([product], {record_key(product):product.sku}, [record_key(product)])
        rows, _ = clean_rows(build_csv_rows([product], 'divina'), structured_item_ids={product.sku} if product.packaging_mode == 'structured_supplier_units' else set())
        return product, rows[0]

    def test_detail_keeps_supplier_identity_and_sheet_but_not_hidden_price(self):
        p, row = self.pipeline(0)
        self.assertEqual(p.sku, '110006')
        self.assertEqual(row['GTIN'], '7611203010012')
        self.assertEqual(row['Brand'], 'Divina Premium')
        self.assertEqual(row['Vessel size'], '1')
        self.assertEqual(row['Vessel unit'], 'kg')
        self.assertIn('Pizzaqualität', row['Category name'])
        self.assertTrue(row['Image'].startswith('https://shop.divinafood.ch/'))
        self.assertIn('id_attachment=1759231', row['Product Sheet'])
        self.assertEqual(row['Price'], '')
        self.assertEqual(row['Status'], 'ACTIVE')

    def test_nested_mini_mozzarella_is_six_cups_of_200g(self):
        p, row = self.pipeline(1)
        self.assertEqual(row['Vessel size'], '200')
        self.assertEqual(row['Vessel unit'], 'g')
        self.assertEqual(row['Bundle size'], '6')
        self.assertEqual(row['Bundle type'], 'CT')
        self.assertEqual(row['GTIN'], '')
        self.assertIn('8002612000272', row['Description'])

    def test_individually_packed_burrata_preserves_carton_count(self):
        p, row = self.pipeline(2)
        self.assertEqual(row['Vessel size'], '125')
        self.assertEqual(row['Bundle size'], '12')
        self.assertEqual(row['Bundle type'], 'CT')
        self.assertIn('Vorbestellung', row['Description'])

    def test_wine_preserves_volume_vintage_region_and_declared_allergen(self):
        product = parse_product_record((ROOT / 'wine.html').read_text(), BASE + '/rose/10000000-wine.html')
        interpret_products([product], {record_key(product):product.sku}, [record_key(product)])
        rows, _ = clean_rows(build_csv_rows([product], 'divina'))
        row = rows[0]
        self.assertEqual(row['Vessel size'], '750')
        self.assertEqual(row['Vessel unit'], 'ml')
        self.assertEqual(row['Vintage'], '2025')
        self.assertEqual(row['Alcohol content'], '13%')
        self.assertEqual(row['Grape variety'], '100% Syrah')
        self.assertEqual(row['Region'], 'Basilicata')
        self.assertEqual(row['Allergens'], 'Sulfite')
        self.assertIn('708019/25', row['Description'])

    def test_fractional_supplier_weight_stays_with_shared_precision_policy(self):
        product = parse_product_record((ROOT / 'fractional.html').read_text(), BASE + '/bohnen/10000000-beans.html')
        interpret_products([product], {record_key(product):product.sku}, [record_key(product)])
        rows, _ = clean_rows(build_csv_rows([product], 'divina'), structured_item_ids={product.sku})
        self.assertEqual(rows[0]['Vessel size'], '2.5')
        self.assertEqual(rows[0]['Vessel unit'], 'kg')
        self.assertEqual(rows[0]['Bundle size'], '6')

    def test_robots_allow_catalog_but_block_specification_download(self):
        robots = 'User-agent: *\nDisallow: /*controller=attachment\nDisallow: /*?order=\n'
        self.assertTrue(robots_allows(robots, BASE + '/2-alle-produkte?page=2'))
        self.assertFalse(robots_allows(robots, BASE + '/index.php?controller=attachment&id_attachment=1'))
        self.assertFalse(robots_allows(robots, BASE + '/2-alle-produkte?order=price'))


class DivinaEmptyDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_listing_is_an_explicit_scrape_failure(self):
        response = SimpleNamespace(ok=True, text=AsyncMock(return_value='User-agent: *\nAllow: /'))
        empty = SimpleNamespace(ok=True, text=AsyncMock(return_value='<html>No products</html>'))
        context = SimpleNamespace(get=AsyncMock(side_effect=[response, empty]), dispose=AsyncMock())
        pw = SimpleNamespace(request=SimpleNamespace(new_context=AsyncMock(return_value=context)))
        manager = AsyncMock()
        manager.__aenter__.return_value = pw
        config = SupplierConfig('divina', False, 'divina', BASE, 'YBM_TOKEN_DIVINA', 'output/divina')
        with TemporaryDirectory() as root, patch('adapters.divina.scraper.async_playwright', return_value=manager), patch('adapters.divina.scraper.get_log_root', return_value=Path(root)), patch('adapters.divina.scraper.asyncio.sleep', new=AsyncMock()):
            result = await DivinaAdapter(config, Path(root)).scrape()
        self.assertEqual(result.products, [])
        self.assertTrue(result.failures)
        self.assertIn('no product links', result.failures[0]['reason'])
