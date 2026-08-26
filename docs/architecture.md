# Architecture

## Purpose

ScrapeMate Hub turns supplier-specific public catalogues into validated YourBarMate catalogues.
Supplier discovery and interpretation are isolated from the shared validation, export, and sync
platform so new suppliers can be added without changing import rules.

## Runtime Topology

```text
Browser -> EC2 control panel -> SQLite job queue / APScheduler
                              -> ECS/Fargate task -> supplier website
                                                   -> YourBarMate API
                              <- CloudWatch job logs and final run summary
```

- **EC2 control plane**: FastAPI dashboard, login, APScheduler, SQLite job history, persistent
  dashboard config, and release metadata.
- **ECS/Fargate worker**: an on-demand task for each scraper job. It has no durable local state.
- **CloudWatch Logs**: live progress, failures, and the `RESULT_RUN_SUMMARY` consumed by the dashboard.
- **AWS Secrets Manager**: production secret store. The dashboard can write supplier token values
  there, but never displays them again.
- **YourBarMate API**: receives only validated `NormalizedProduct` data.

The production backend is `ecs_fargate`; `local_subprocess` is for local development and controlled
diagnosis only.

## Code Boundaries

| Area | Responsibility |
| --- | --- |
| `adapters/<supplier>/scraper.py` | Public catalogue discovery, fetches, supplier-specific failures, live progress |
| `adapters/<supplier>/transform.py` | Supplier-specific parsing and interpretation into `NormalizedProduct` |
| `core/`, `cleaner.py`, `validate.py`, `export.py`, `ybm.py` | Reusable cleaning, validation, exports, IDs, and API synchronization |
| `orchestrator.py` | Scrape, validation, export, and sync stage orchestration |
| `webapp/` | Operations UI, queue, scheduler, authentication, and ECS task tracking |
| `deploy/` and `.github/workflows/` | EC2 deployment and Fargate worker-image delivery |

The shared stage contract is `RawSupplierRecord -> SupplierInterpretedRecord -> NormalizedProduct`.
Only `NormalizedProduct` reaches shared validation/export/sync. Do not move supplier packaging,
category, or website behavior into shared code unless it is genuinely reusable.

## Job Modes And Data Flow

- **Scrape**: discover URLs, fetch detail pages, transform records, and write local export artifacts.
- **Dry run**: scrape plus validation and a non-writing sync diff.
- **Scrape + sync**: scrape, validate, export, then create/update/inactivate YourBarMate records.
- **Sync latest export**: validate/sync a previously exported normalized dataset without re-scraping.

Every adapter must log monotonic progress:

```text
PROGRESS phase=discovering found=<n> pages=<n> expected=<n>
PROGRESS phase=processing found=<n> processed=<n> scraped=<n> total=<n>
```

Fargate output directories are ephemeral. The dashboard persists job metadata and copies the final
run/sync summary from CloudWatch. Do not assume raw worker files are retained unless a durable
artifact store is explicitly configured.

## Configuration And State Ownership

| Data | Owner | Rule |
| --- | --- | --- |
| Adapter source, shared code, tests, fixtures, deploy assets | Git | Changed through PRs only |
| `suppliers.json` | Git | Defaults for newly introduced suppliers |
| Live supplier config, schedules, enabled/archived status, catalog policy | Dashboard persistent state | Never overwritten by deployment |
| Tokens and app secrets | AWS Secrets Manager / runtime environment | Never commit or print values |
| Job history and release metadata | EC2 persistent state | Back up; do not hand-edit as source code |

During deployment, `deploy/aws/merge_supplier_configs.py` adds supplier definitions missing from
the live dashboard configuration while preserving every existing live setting. This is why a
dashboard schedule change survives future Git deployments.

## Quality Gate

The enforceable contributor rules are in [AGENTS.md](../AGENTS.md). For every supplier adapter:

1. Keep supplier code under `adapters/<supplier>/`.
2. Implement both standard progress phases.
3. Fail clearly if discovery is empty, or implement and document a public fallback.
4. Add listing and detail fixtures plus supplier-scoped tests.
5. Ensure the adapter runs in a clean Fargate task without local-machine dependencies.

Repository hygiene is also enforced in CI. It rejects tracked local secrets and generated files,
and requires the authoritative runbooks to remain present. Contributors must remove or update stale
source, fixtures, tests, documentation, and defaults as part of the feature that obsoletes them.

Run all tests before opening a PR:

```bash
.venv/bin/python -m unittest discover -s tests -v
```
