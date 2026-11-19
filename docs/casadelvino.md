# Casa del Vino

The German public catalog is discovered through the request used by the
shop's search interface, `POST /api/artikelsearch/search`. The initial
HTML supplies the search parameters and expected count. Advance by the
number of returned records, deduplicate by shop article ID, and report
a discovery failure if the full count does not reconcile. Every product
detail is fetched and matched to the supplier SKU before export.

Listing and detail fields cover supplier SKU, public availability,
producer, region, country, grape varieties, vintage, ABV, image, barcode,
standard bottle sizes, multipack sizes and price-free factsheet links.
The HTML supplies tasting notes, terroir, vinification, producer history,
food pairings, serving temperature, aging potential, allergens, awards,
and explicit organic and vegan badges. Preserve optional details in the
description when the current YourBarMate product payload lacks an
individual structured field. Product page links in other languages are
available in raw specs.

A bottle listing uses the labeled carton or wooden case size where
shown. A separately listed sale unit such as six bottles of 75 cl keeps
the published inner bottle size and count. The product barcode on
such an offer applies to the sellable set. Unmeasured accessories are
represented as one unit. The shared cleaner handles precision.

## Price provenance

The anonymous storefront exposes a CHF offer price in the search
response and the product HTML. Preserve the public offer, list price
and sale-unit basis in the raw export. Regular prices linked to shop
promotions are captured when present. The shop's `istNetto` flag is
an article classification. It is not a net purchase price. The supplier
states that individual trade conditions are supplied via the company
account and enabled by its sales team. Leave the Connect purchase price
empty until confirmed conditions are supplied. The adapter uses public
catalog requests only.

Discovery and detail fixtures cover pagination, incomplete catalogs,
SKU matching, organic and vegan badges, bottle size, units in a case,
GTIN, factsheets and separation of public prices from purchase prices.
The supplier defaults to disabled scheduling with `keep_existing`.
