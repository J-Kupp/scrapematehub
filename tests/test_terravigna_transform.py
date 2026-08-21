from __future__ import annotations

import unittest

from adapters.terravigna.transform import (
    extract_listing_product_total,
    extract_next_listing_url,
    extract_product_links,
    extract_sitemap_product_links,
    parse_product_record,
)


class TerraVignaTransformTests(unittest.TestCase):
    def test_listing_helpers_read_product_urls_pagination_and_total(self) -> None:
        html = """
        <p class="toolbar-amount">Artikel 1-30 von 1'414</p>
        <ol>
          <li class="product-item"><a class="product-teaser__image" href="/wine-one">Wine</a></li>
          <li class="product-item"><a class="product-teaser__image" href="https://www.terravigna.ch/wine-two">Wine</a></li>
        </ol>
        <li class="pages-item-next"><a href="/shop?p=2">Weiter</a></li>
        """
        self.assertEqual(extract_listing_product_total(html), 1414)
        self.assertEqual(
            extract_product_links(html, "https://www.terravigna.ch"),
            ["https://www.terravigna.ch/wine-one", "https://www.terravigna.ch/wine-two"],
        )
        self.assertEqual(
            extract_next_listing_url(html, "https://www.terravigna.ch"),
            "https://www.terravigna.ch/shop?p=2",
        )

    def test_sitemap_discovery_keeps_only_image_bearing_product_entries(self) -> None:
        sitemap = """
        <urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
          <url><loc>https://www.terravigna.ch/about</loc></url>
          <url><loc>https://www.terravigna.ch/wine-one</loc><image:image><image:loc>https://cdn.example/wine.jpg</image:loc></image:image></url>
        </urlset>
        """
        self.assertEqual(
            extract_sitemap_product_links(sitemap, "https://www.terravigna.ch"),
            ["https://www.terravigna.ch/wine-one"],
        )

    def test_parse_product_record_enriches_listing_data_from_detail_page(self) -> None:
        html = """
        <html><head>
          <meta property="og:image" content="/media/wine.png" />
        </head><body>
          <div class="breadcrumbs"><li><a>Shop</a></li><li><a>Schaumwein</a></li><li class="product"><strong>Example</strong></li></div>
          <h1 class="page-title"><span class="base">Example Prosecco DOCG</span></h1>
          <h4 class="producer__name">Example Estate</h4>
          <form data-product-sku="TV-20122"></form>
          <span data-price-type="finalPrice" data-price-amount="14.50"></span>
          <a data-simple-product-id="1">75 cl</a><a data-simple-product-id="2">150 cl</a>
          <div class="product-detail__attributes"><ul>
            <li class="region"><span class="content">Veneto | Italien</span><div class="overlay-flag"><span>Herkunft</span></div></li>
            <li><span>Glera</span><div class="overlay-flag"><span>Rebsorten</span></div></li>
            <li><span class="content">11.5 % Vol.</span><div class="overlay-flag"><span>Alkoholgehalt</span></div></li>
          </ul></div>
          <div class="further-info__item"><div class="further-info__title"><h3>Vinifikation</h3></div><div class="further-info__content">Methode Charmat</div></div>
        </body></html>
        """
        product = parse_product_record(html, "https://www.terravigna.ch/example-prosecco")
        assert product is not None
        self.assertEqual(product.sku, "TV-20122")
        self.assertEqual(product.price, "14.50")
        self.assertEqual(product.image_url, "https://www.terravigna.ch/media/wine.png")
        self.assertEqual(product.category_path, "Schaumwein")
        self.assertEqual(product.region, "Veneto")
        self.assertEqual(product.country, "Italien")
        self.assertEqual(product.vessel_size, "75")
        self.assertEqual(product.vessel_unit, "cl")
        self.assertEqual(product.specs["rebsorten"], "Glera")
        self.assertEqual(product.specs["vinifikation"], "Methode Charmat")
