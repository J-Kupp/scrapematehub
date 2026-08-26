# ScrapeMate Hub Contribution Rules

## Documentation first

Before changing code or infrastructure, read the relevant authoritative runbook:
- `docs/architecture.md`: boundaries, data flow, and sources of truth
- `docs/new-supplier.md`: supplier-adapter implementation checklist
- `docs/aws-runbook.md`: AWS services, secrets, and runtime ownership
- `docs/deployment-and-rollback.md`: delivery and recovery procedure
- `docs/troubleshooting.md`: safe diagnosis of common production problems

Do not treat generated artifacts, local caches, or the EC2 application directory as source code.

## Supplier adapter quality gate

Every supplier adapter must meet these requirements before it can be merged:

1. Keep supplier-specific discovery and parsing in `adapters/<supplier>/`; emit only `NormalizedProduct` records to shared code.
2. Emit absolute, monotonic log markers in both phases:
   - `PROGRESS phase=discovering found=<n> pages=<n> expected=<n>`
   - `PROGRESS phase=processing found=<n> processed=<n> scraped=<n> total=<n>`
3. Never complete a scrape with zero discovered products silently. Record a clear failure, or use a documented public fallback such as a sitemap.
4. Add fixture-based tests for product discovery and detail transformation. Tests must prove non-zero discovery from a representative listing fixture.
5. Ensure the adapter works in the ephemeral Fargate worker: no reliance on local cache, local source config, or files outside the configured runtime paths.

## Delivery workflow

Use `codex/<topic>` branches. Every change follows `commit -> push -> PR -> CI -> merge -> deploy -> live verification`.
Do not edit production source code directly on EC2. Dashboard settings and secrets are persistent runtime state and are not committed to Git.
