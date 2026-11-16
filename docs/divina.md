# Divina supplier adapter

Source: https://shop.divinafood.ch/2-alle-produkte (public PrestaShop catalog).

## Discovery and detail mapping

Walk the public category pagination, deduplicating by PrestaShop product ID. Fetch every product
page and read its embedded product payload plus visible breadcrumb, EAN and attachment links.
The supplier article reference is distinct from the internal shop ID. Combination suffixes in
URLs are removed for canonical identity; the default public sale option is imported once.

Preserve the supplier SKU, brand, image, category, description and every published feature.
Wine features additionally populate region, vintage, grape variety, and alcohol-content columns.
Explicit allergen statements in Zusatzinfo populate Allergens. No dietary or allergen claims are
inferred from a product name. Optional details also remain in the description for the existing
sync mapper, which does not support every CSV property as its own structured field.

Names with slash-containing supplier references retain the original reference in description and
raw specs. The existing shared import-ID sanitization still applies to the CSV Item ID.

## Packaging and barcode decisions

Explicit physical measures and multipack counts from product names are protected from subsequent
generic title inference using structured_supplier_units. For example, 10 x 20 g, 6 Becher/Krt.
becomes six units of 200 g; 125 g, Krt. à 12 Schalen becomes twelve units of 125 g.
Keep fractional supplier weights unchanged until shared precision normalization.
If the name does not establish packaging, the existing shared interpreter uses the published
features and leaves unresolved quantities as the documented single-item fallback.

The page's EAN identifies the offered article. If an inner/outer barcode assignment is not
established for a multipack, preserve it explicitly as the source article GTIN in description
and specs rather than assigning it to an unverified inner vessel. All-zero placeholder EANs
are excluded from the GTIN column.

## Public-source limitations

The anonymous shop hides prices, despite numerical amounts in HTML attributes and JSON-LD.
Do not treat those amounts as confirmed purchase prices. Prices remain empty. Login-gated
order buttons and stock counters do not establish stock availability for a customer; the
adapter imports listed products as ACTIVE without changing their ordering permissions.

robots.txt permits catalog pages and normal page=N pagination, but explicitly disallows the
controller=attachment download route. Keep specification URLs, but do not crawl these PDFs.
The 204-page detail sample contained no HTML ingredient lists or nutritional tables. These
fields remain empty unless explicitly present on a future public detail page. The linked
specification is the available source for manual follow-up.

## Verification, 2026-11-16

- All 14 listing pages discover exactly 856 distinct product IDs.
- Sample 204 product pages using one representative per 134 category URL paths plus regularly
  spaced catalog entries; all sample fetches and transformations succeeded.
- Transform, packaging interpreter, export, cleaner, and validation: zero errors and warnings.
- All 204 sample rows have image, brand and description; 183 link a specification; 22 provide
  an explicit allergen statement. Optional missing source data remains empty.
- Fixtures cover listing pagination, ordinary details, hidden prices, nested packaging,
  individually packed burrata and wine attributes. Tests cover explicit empty discovery,
  robots restrictions and fractional weights.

The default supplier configuration is disabled, with a disabled schedule, keep_existing policy,
and token variable YBM_TOKEN_DIVINA. Deployment adds the supplier while preserving existing
runtime settings. Configure a supplier-specific token before any first YourBarMate sync.
