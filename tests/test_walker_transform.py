from __future__ import annotations

import unittest

from adapters.walker.transform import (
    extract_category_links,
    extract_next_listing_url,
    extract_product_links,
    parse_product_record,
    product_candidate_from_url,
)


class WalkerTransformTests(unittest.TestCase):
    def test_product_candidate_from_url_accepts_walker_detail_url(self) -> None:
        self.assertTrue(
            product_candidate_from_url(
                "https://shop.walker.swiss/de/Alle-Produkte/Obst-und-Gemuese/Gemuese/Gurken-12668.html"
            )
        )

    def test_extract_product_links_deduplicates_card_links(self) -> None:
        html = """
        <article class="article-list-item">
          <a href="/de/Alle-Produkte/Foo-123.html"></a>
          <a href="/de/Alle-Produkte/Foo-123.html">Foo</a>
        </article>
        <article class="article-list-item">
          <a href="/de/Alle-Produkte/Bar-456.html">Bar</a>
        </article>
        """

        links = extract_product_links(html, "https://shop.walker.swiss")

        self.assertEqual(
            links,
            [
                "https://shop.walker.swiss/de/Alle-Produkte/Bar-456.html",
                "https://shop.walker.swiss/de/Alle-Produkte/Foo-123.html",
            ],
        )

    def test_extract_category_links_keeps_category_pages_and_excludes_products(self) -> None:
        html = """
        <nav>
          <a href="/de/alle-produkte/milchprodukte/">Milchprodukte</a>
          <a href="/de/alle-produkte/milchprodukte/kaese/">Käse</a>
          <a href="/de/alle-produkte/milchprodukte/kaese/Raclette-123.html">Product</a>
          <a href="/de/guide/">Guide</a>
        </nav>
        """

        self.assertEqual(
            extract_category_links(html, "https://shop.walker.swiss"),
            [
                "https://shop.walker.swiss/de/alle-produkte/milchprodukte/",
                "https://shop.walker.swiss/de/alle-produkte/milchprodukte/kaese/",
            ],
        )

    def test_extract_next_listing_url_reads_load_more_target(self) -> None:
        html = '<button data-op-href="https://shop.walker.swiss/de/alle-produkte/?pposCatItem=25"></button>'
        self.assertEqual(
            extract_next_listing_url(html, "https://shop.walker.swiss"),
            "https://shop.walker.swiss/de/alle-produkte/?pposCatItem=25",
        )

    def test_parse_product_record_includes_external_page_enrichment(self) -> None:
        product_html = """
        <nav class="opc-breadcrumb">
          <ol class="breadcrumb-navigation">
            <li><a href="/de/start.htm">Home</a></li>
            <li><a href="/de/alle-produkte/">Alle Produkte</a></li>
            <li><a href="/de/alle-produkte/obst-und-gemuese/">Obst und Gemüse</a></li>
            <li class="is-active">Feigen</li>
          </ol>
        </nav>
        <div class="article-image">
          <img src="/CatCache/catcache.1/pictures/999/999_M_1.jpg" />
        </div>
        <div class="article-infos">
          <h1>Feigen 3 kg</h1>
          <dl class="article-spec-infos">
            <dt>Artikelnummer</dt><dd>140165</dd>
            <dt>Label</dt><dd>Vegan<br>Glutenfrei</dd>
            <dt>Herkunft</dt><dd>TR</dd>
            <dt>Link</dt><dd><a href="https://example.com/feigen">Herstellerlink</a></dd>
          </dl>
        </div>
        <div class="accordion-body">
          <p>Die hinterlegten Bilder dienen nur als Referenz.</p>
        </div>
        """
        external_html = """
        <html>
          <head>
            <title>Masserey Feigen</title>
            <meta name="description" content="Süsse Feigen mit ausgewählter Qualität." />
            <meta property="og:image" content="https://example.com/images/feigen.jpg" />
          </head>
          <body>
            <main>
              <p>Feigen für Gastronomie und Handel.</p>
              <a href="/downloads/spec.pdf">Spezifikation</a>
            </main>
          </body>
        </html>
        """

        parsed = parse_product_record(
            product_html,
            "https://shop.walker.swiss/de/Alle-Produkte/Obst-und-Gemuese/Feigen-999.html",
            external_html=external_html,
            external_url="https://example.com/feigen",
        )

        assert parsed is not None
        self.assertEqual(parsed.sku, "140165")
        self.assertEqual(parsed.category_path, "Alle Produkte > Obst und Gemüse")
        self.assertEqual(parsed.country, "TR")
        self.assertEqual(parsed.image_url, "https://shop.walker.swiss/CatCache/catcache.1/pictures/999/999_M_1.jpg")
        self.assertEqual(parsed.vessel_size, "3")
        self.assertEqual(parsed.vessel_unit, "kg")
        self.assertEqual(parsed.labels, ["Vegan", "Glutenfrei"])
        self.assertEqual(parsed.product_sheet_url, "https://example.com/downloads/spec.pdf")
        self.assertIn("Süsse Feigen", parsed.description)
        self.assertEqual(parsed.manufacturer, "Masserey Feigen")
        self.assertEqual(parsed.specs["manufacturer_link"], "https://example.com/feigen")
