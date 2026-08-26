# New Supplier

This is the required procedure for adding or changing a supplier adapter. Follow it together with
[AGENTS.md](../AGENTS.md) and [Architecture](architecture.md).

## Before Writing Code

1. Confirm that the public supplier pages may be scraped and identify robots, login, rate-limit,
   pagination, sitemap, menu, and API behavior.
2. Define the expected supplier slug using lowercase ASCII and hyphens only when needed.
3. Decide how products are discovered and how a zero-result discovery will fail or fall back.
4. Identify stable product identity, detail-page fields, images, category path, and packaging evidence.

## Required Files

Create all of these in the same PR:

```text
adapters/<supplier>/__init__.py
adapters/<supplier>/scraper.py
adapters/<supplier>/transform.py
adapters/<supplier>/fixtures/<representative listing and detail samples>
tests/test_<supplier>_transform.py
```

Then register the adapter in `adapters/__init__.py` and add a disabled default entry to
`suppliers.json`. Include the expected token environment-variable name, output path, safe catalog
policy, disabled schedule, and conservative scraping settings.

## Implementation Rules

- The scraper discovers and fetches supplier truth. The transformer parses supplier-specific
  fields and returns only `NormalizedProduct` instances.
- De-duplicate product detail URLs using the supplier's stable product identifier.
- Use absolute canonical URLs and persist useful diagnostics/failures in `SupplierScrapeResult`.
- Emit both `PROGRESS` marker formats on every run. Counters must never decrease.
- Bound concurrency and add a small randomized request delay. Do not depend on a local browser,
  local cache, local config file, or files outside configured runtime roots.
- Do not place shared validation/import policy inside the adapter.

## Tests Required

Tests must use committed fixtures and prove:

1. A representative listing discovers at least one product URL.
2. Pagination/menu/sitemap fallback behavior works where applicable.
3. A detail fixture maps important fields correctly: SKU, name, canonical URL, category, image,
   packaging, and supplier-specific critical data.
4. Empty discovery produces an explicit failure or a tested fallback.

Run the full suite locally:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Dashboard Onboarding

The dashboard is for operations, not source-code authoring.

1. After code is merged and deployed, open **Connections** or **Add supplier**.
2. Confirm the adapter, transformer, fixtures/tests, and expected secret are detected.
3. Add the supplier token through **Edit supplier**. It is stored in the configured shared secrets
   backend and is not shown again.
4. Keep the schedule disabled until a Fargate scrape/dry-run validates the expected catalogue size.
5. Choose the catalog policy deliberately:
   - `delete_missing`: products missing from a complete new supplier run become inactive.
   - `keep_existing`: update matching products and add new ones, without inactivating old products.
6. Run a scrape first, review failures/corrections, then perform the first sync.

## Delivery Checklist

```text
codex/<topic> branch
-> implement adapter + fixtures + tests + default config
-> local tests
-> commit and push
-> PR and CI
-> merge to main
-> verify EC2 deploy and worker image build
-> run a limited Fargate smoke test
-> configure token/schedule in dashboard
-> perform first controlled sync
```

Never repair deployed source over SSH. If the live adapter is wrong, make a new PR and redeploy it.
