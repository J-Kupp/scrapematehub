# ScrapeMate Hub Contribution Rules

## Documentation first

Before changing code or infrastructure, read the relevant authoritative runbook:
- `docs/architecture.md`: boundaries, data flow, and sources of truth
- `docs/new-supplier.md`: supplier-adapter implementation checklist
- `docs/aws-runbook.md`: AWS services, secrets, and runtime ownership
- `docs/deployment-and-rollback.md`: delivery and recovery procedure
- `docs/troubleshooting.md`: safe diagnosis of common production problems

Do not treat generated artifacts, local caches, or the EC2 application directory as source code.

## Repository hygiene

Repository hygiene is part of every change, not a separate cleanup project:

1. Before work, inspect `git status --short` and preserve unrelated user changes.
2. While changing a feature, remove or update stale code, tests, docs, comments, and configuration
   that the feature makes obsolete.
3. Do not add generated output, caches, browser data, local environment files, credentials, or
   machine-specific paths to Git.
4. Before every commit, run `.venv/bin/python tools/check_repo_hygiene.py` and the full test suite.
5. If cleanup would delete runtime data or change a production setting, leave it untouched and
   document/escalate it instead of treating it as source cleanup.

## Supplier adapter quality gate

Every supplier adapter must meet these requirements before it can be merged:

1. Keep supplier-specific discovery and parsing in `adapters/<supplier>/`; emit only `NormalizedProduct` records to shared code.
2. Emit absolute, monotonic log markers in both phases:
   - `PROGRESS phase=discovering found=<n> pages=<n> expected=<n>`
   - `PROGRESS phase=processing found=<n> processed=<n> scraped=<n> total=<n>`
3. Never complete a scrape with zero discovered products silently. Record a clear failure, or use a documented public fallback such as a sitemap.
4. Add fixture-based tests for product discovery and detail transformation. Tests must prove non-zero discovery from a representative listing fixture.
5. Ensure the adapter works in the ephemeral Fargate worker: no reliance on local cache, local source config, or files outside the configured runtime paths.
6. Keep vessel sizes as supplier truth in the adapter, but rely on the shared cleaner for the
   YourBarMate precision contract. Supplier tests must cover any unusual packaging size, including
   fractions and sub-unit values, so the shared normalization is exercised before a live sync.

## Shared vessel-size contract

YourBarMate vessel sizes use unit-specific precision. This is shared import policy, never
supplier-adapter logic:

| Unit | Maximum decimal places |
| --- | --- |
| `g`, `ml`, `quantity` | 0 |
| `cl` | 1 |
| `dl` | 2 |
| `kg`, `l` | 3 |

The cleaner uses half-up rounding. Positive values that would otherwise round to zero are raised
to the smallest representable value, for example `0.0005 kg -> 0.001 kg`. The validator enforces
the same contract before sync. Do not bypass the cleaner or recreate these rules in an adapter.

## Delivery workflow

Use `codex/<topic>` branches. Every change follows `commit -> push -> PR -> CI -> merge -> deploy -> live verification`.
Do not edit production source code directly on EC2. Dashboard settings and secrets are persistent runtime state and are not committed to Git.
