# Merat catalog mapping

The adapter reads anonymous German listing and product-detail JSON from the public shop API.
Each product detail is fetched; no authenticated session or customer prices are required.

## Detail mapping

- Preserve published SKU, canonical product link, image, ingredients, declared allergens,
  storage instructions, ISO production country, and primary listing category.
- Export all published nutrient values with their reference quantity and the supplier product-sheet URL.
- Export French, Italian, and English names in their dedicated CSV columns.
- Preserve certifications, origin, fish production method, trace-allergen warnings, transport,
  and seasonal information in the description. Certifications are not dietary labels.
- Keep nutrient values, product-sheet links and translated names in the description too, since
  the current sync mapper does not support all optional CSV fields as structured properties.
- Do not use unpublished price-policy fields as purchase prices. The anonymous price is usually absent.
- The source sometimes sets `merat_vegan=1` on meat products. This ambiguous internal flag is not
  used to claim vegan suitability. Explicit dietary certification labels remain eligible.

## Packaging

Select the source base ordering unit. Weight-based sale units preserve their published weight.
Count bundles with an explicit inner piece unit use the piece's published net weight and the
published bundle count, with matching inner GTIN and outer GTIN. Cartons retain type CT.
Other sale-unit options remain in raw record specs.

`structured_supplier_units` marks authoritative source packaging. The shared interpreter preserves
it, and the final cleaner skips title-based packaging inference for those item IDs while still
applying shared precision, numeric normalization, and validation.

## Verification, 2026-11-16

Read all 2,143 listing records and select category representatives plus regularly spaced records.
Successfully fetched and checked 129 product details across 80 listing categories. One selected
listing had no item number and returned 404 when probed; it was excluded from the detail sample.
This is a sample audit, not a successful full scrape claim.

Run the sample through transformation, the shared packaging interpreter, CSV export, cleaning,
and validation: zero errors and zero warnings. Coverage:

| Field | Present out of 129 |
| --- | ---: |
| Ingredients and declared allergens | 129 |
| Nutritional values with source reference quantity | 127 |
| Product sheet | 129 |
| Each of FR, IT, EN names | 129 |
| Country | 125 |
| Image | 125 |
| Explicit count bundle | 9 |

Missing source values remain empty. Regression fixtures cover fractional weights, a 14 x 60 g
carton with distinct inner and outer GTINs, fish production, and trace-allergen warnings.
