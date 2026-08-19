# Supplier Scraper + YourBarMate Sync Platform

This project now provides a reusable local sync platform for supplier catalog scrapers.

The first implemented supplier is Swissbox AG:
- scrape publicly visible product data
- normalize it into one shared product model
- export CSV + raw JSONL + failure report + correction report
- validate the export
- sync validated products directly to the supplier's YourBarMate account
- run weekly on macOS via `launchd`
- expose an internal web control panel for runs, syncs, logs, and artifacts

## Architecture

The platform now has a strict split between supplier-specific code and shared platform code.

Shared platform areas:
- `scraper.py`: CLI entrypoint
- `orchestrator.py`: run flow for scrape -> validate -> export -> sync
- `models.py`: normalized product, supplier config, validation, and sync result models
- `config.py`: supplier config loading, `.env` loading, and shared secrets bootstrap
- `core/`: shared contracts and platform re-export modules
- `ybm.py`: YourBarMate ID mapping, payload mapping, API client, and sync logic
- `webapp/`: FastAPI control panel, onboarding views, queue, settings, and artifact access
- ECS/Fargate worker jobs currently run at `4 vCPU / 8 GB` to keep browser scraping responsive while still being on-demand

Supplier code areas:
- `adapters/base.py`: supplier adapter interface
- `adapters/<supplier_slug>/scraper.py`: discovery + scraping
- `adapters/<supplier_slug>/transform.py`: supplier-specific transformation/interpreter logic
- `adapters/<supplier_slug>/fixtures/`: supplier fixtures and reference samples
- `tests/test_<supplier_slug>_transform.py`: supplier-scoped transformation coverage

Swissbox currently lives in:
- `adapters/swissbox/scraper.py`
- `adapters/swissbox/transform.py`
- `adapters/swissbox/fixtures/`

Shared contracts:
- `RawSupplierRecord`
- `SupplierInterpretedRecord`
- `NormalizedProduct`

Rules of the platform:
- scrapers extract raw supplier truth only
- supplier-specific packaging/category/business interpretation stays in that supplier transformer
- only `NormalizedProduct` enters shared validation, export, and API sync
- shared import rules never live in supplier adapters unless they are truly reusable platform rules

Per supplier runtime artifacts:
- `output/<supplier>/...`: CSV, raw JSONL, failures, diagnostics, correction report, run summary, sync report
- `logs/<supplier>/scrape.log`: supplier scrape log
- `logs/service.log`: cross-supplier orchestration log
- `state/<supplier>/state.json`: last successful run metadata
- `state/<supplier>/remote_products.json`: latest remote product snapshot
- `.cache/<supplier>/manifest.jsonl`: HTTP snapshot cache index

## Install

1. Create a virtual environment:

```bash
python3 -m venv .venv
```

2. Install dependencies:

```bash
.venv/bin/pip install -r requirements.txt
```

3. Install Chromium for Playwright:

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" .venv/bin/python -m playwright install chromium
```

## Configuration

### Supplier config

The runner loads supplier definitions from:
- `suppliers.json` if present
- otherwise the built-in default config for Swissbox

Use [suppliers.example.json](/Users/jakobJakob/Documents/New%20project%202/suppliers.example.json) as the template for new suppliers.

Each supplier config entry defines:
- `supplier_slug`
- `enabled`
- `scraper_adapter`
- `base_url`
- `ybm_api_base`
- `ybm_token_env_var`
- `output_dir`
- `schedule`
- `scrape_settings`

Important team workflow note:
- you can create a supplier config in the control panel before the adapter code exists
- that supplier will appear in onboarding as `Config only` or `Needs secret` until the code and secrets are added

### Secrets

Copy `.env.example` to `.env.local` and add the correct token for each supplier account:

```bash
cp .env.example .env.local
```

Example:

```env
YBM_TOKEN_SWISSBOX=your_real_token_here
CONTROL_PANEL_SESSION_SECRET=long_random_secret
CONTROL_PANEL_ADMIN_PASSWORD=strong_admin_password
SUPPLIER_OUTPUT_ROOT=
SCRAPER_STATE_ROOT=
SCRAPER_LOG_ROOT=
SCRAPER_CACHE_ROOT=
PLAYWRIGHT_BROWSERS_PATH=
```

The token value is never stored in the supplier config.

Optional shared secrets mode:

```env
SHARED_SECRETS_BACKEND=aws-secrets-manager
AWS_SECRETS_MANAGER_SECRET_ID=yourbarmate-suppliers-prod
AWS_REGION=eu-central-1
```

When enabled, the app loads a JSON object from AWS Secrets Manager and injects any missing env vars from that secret at startup. The control panel only shows secret presence/health, never secret values.

### Control panel config

Use [control_panel.example.json](/Users/jakobJakob/Documents/New%20project%202/control_panel.example.json) as the template for a local `control_panel.json`.

It defines:
- listen host/port
- SQLite DB path
- env file to load on startup
- scheduler mode
- session/proxy hardening flags
- artifact roots exposed read-only in the UI
- bootstrap admin accounts backed by password env vars

## CLI

Run one supplier end to end:

```bash
.venv/bin/python scraper.py run-supplier swissbox --env-file .env.local
```

Run all enabled suppliers:

```bash
.venv/bin/python scraper.py run-all-suppliers --env-file .env.local
```

Compute the sync diff without writing to YourBarMate:

```bash
.venv/bin/python scraper.py dry-run-supplier swissbox --env-file .env.local
```

Sync from the latest normalized JSONL export without re-scraping:

```bash
.venv/bin/python scraper.py sync-from-export swissbox --env-file .env.local
```

Optional cache bypass:

```bash
.venv/bin/python scraper.py run-supplier swissbox --env-file .env.local --force-refresh
```

Clean an existing CSV and write a correction report:

```bash
.venv/bin/python scraper.py clean-csv input.csv cleaned.csv corrections.csv
```

Start the internal control panel:

```bash
.venv/bin/python scraper.py run-control-panel
```

Or point it at a custom config:

```bash
.venv/bin/python scraper.py run-control-panel --config control_panel.json
```

The control panel defaults to [http://127.0.0.1:8765](http://127.0.0.1:8765).

## Control Panel

The v1 control panel is a small internal FastAPI app with server-rendered pages.

Pages:
- Dashboard: supplier list, latest run/sync status, quick actions
- Connections: supplier onboarding readiness, adapter/transformer presence, secret presence, fixtures/tests, and schedule visibility
- Supplier detail: artifacts, latest jobs, config and schedule summary
- Job detail: queued/running/completed state with log tail
- System: health, connection readiness, scheduler mode, artifact roots

Actions exposed from the UI:
- `Dry Run`
- `Run Scrape + Sync`
- `Sync Latest Export`

Operational defaults:
- one global worker
- FIFO queue
- one running job total at a time
- duplicate queued/running job per supplier and job type is rejected
- scheduled and manual jobs share the same queue/history

Execution backends:
- `local_subprocess`: current default, runs scraper jobs on the same host as the control panel
- `ecs_fargate`: launches an ECS task per job and tracks remote task metadata in the control panel DB

The long-term cost-optimized target is:
- small always-on control plane
- ECS/Fargate workers for actual scraper execution
- S3-backed run artifacts

Authentication:
- simple username/password login
- bootstrap admin user created from `CONTROL_PANEL_ADMIN_PASSWORD`
- all current teammates are admins in v1
- signed cookie session auth
- secrets stay server-side and are never editable in the UI
- production mode can require non-placeholder secrets and HTTPS-only cookies

Onboarding states shown in the UI:
- `Config only`
- `Needs secret`
- `Implementation pending`
- `Ready for sync`
- `Live`

Checklist states tracked per supplier:
- Config created
- Secret available
- Adapter implemented
- Transformer implemented
- Fixtures/tests present
- Dry run passed
- Validation passed
- First sync passed

Artifacts exposed read-only through the UI:
- CSV
- raw JSONL
- failures JSONL
- corrections CSV
- packaging audit
- run summary
- sync report
- scrape/service logs

## Swissbox Notes

The Swissbox adapter:
- checks and respects `robots.txt`
- stores fetched HTML/XML/GZip snapshots locally
- uses sitemap plus public category discovery
- avoids disallowed widget/query URLs
- exports one row per normalized purchasable product variant

Important Swissbox paths:
- [output/swissbox/swissbox_products.csv](/Users/jakobJakob/Documents/New%20project%202/output/swissbox/swissbox_products.csv)
- [output/swissbox/swissbox_products_raw.jsonl](/Users/jakobJakob/Documents/New%20project%202/output/swissbox/swissbox_products_raw.jsonl)
- [output/swissbox/swissbox_products_failures.jsonl](/Users/jakobJakob/Documents/New%20project%202/output/swissbox/swissbox_products_failures.jsonl)
- [output/swissbox/swissbox_corrections.csv](/Users/jakobJakob/Documents/New%20project%202/output/swissbox/swissbox_corrections.csv)
- [logs/swissbox/scrape.log](/Users/jakobJakob/Documents/New%20project%202/logs/swissbox/scrape.log)

Current top-level Swissbox artifacts from the earlier one-off export are:
- [output/swissbox_products.csv](/Users/jakobJakob/Documents/New%20project%202/output/swissbox_products.csv)
- [output/swissbox_products_corrections.csv](/Users/jakobJakob/Documents/New%20project%202/output/swissbox_products_corrections.csv)

## YourBarMate Sync Behavior

Implemented defaults:
- one separate YourBarMate account/token per supplier
- stable category IDs derived from category path
- cleaned, URL-safe product IDs derived from the exported `Item ID`
- automatic sync after validation
- missing supplier products are marked `INACTIVE`
- product deletion is never used

Sync actions:
- create missing categories
- rename categories when IDs match but names change
- create missing products
- patch changed products
- skip unchanged products
- set disappeared managed products to `INACTIVE`

Ownership rule:
- because each supplier has its own YourBarMate account, the job manages all products in that target supplier account

## Team Workflow

The intended collaboration model is:
- supplier config and run operations happen in the control panel
- scraper and transformer implementation happens in Git
- every new supplier is added through a branch and pull request
- production EC2 is a deploy target only, not a coding environment

Recommended supplier PR checklist:
- adapter package added under `adapters/<supplier_slug>/`
- `scraper.py` implemented
- `transform.py` implemented
- fixtures added
- supplier transform tests added
- dry-run or fixture-based validation coverage included
- supplier-specific caveats documented

The control panel can be used before code exists:
- create the supplier config and schedule
- assign the expected token env var
- use the Connections page to see what is still missing

## Resume After Interruption

Re-run the same command. Existing snapshots are reused from:
- `output/<supplier>/raw_html/`
- `output/<supplier>/raw_json/`
- `.cache/<supplier>/manifest.jsonl`

This makes restarts much faster and safer after interruptions.

## GitHub Actions and AWS Deployment

The repo now includes:
- `.github/workflows/ci.yml`: run compile + unit tests on pull requests and pushes
- `.github/workflows/deploy.yml`: rerun tests on `main`, sync the repo to EC2, restart the app service, and verify `/healthz`
- `deploy/aws/deploy-from-git.sh`: remote deploy helper used by the EC2 host

The deploy helper now also writes release metadata into the control-panel state directory so the System page can show:
- deployed revision
- deploy timestamp
- deploy source
- host name

Expected GitHub Actions secrets:
- `EC2_HOST`
- `EC2_USER`
- `EC2_APP_DIR`
- `EC2_SSH_PRIVATE_KEY`

Recommended team workflow:
1. create or update supplier code in a branch
2. open a pull request
3. let CI run tests
4. merge to `main`
5. let the deploy workflow update the EC2 control panel

The EC2 environment can either:
- keep using a local env file, or
- load secrets from AWS Secrets Manager via `SHARED_SECRETS_BACKEND=aws-secrets-manager`

## ECS/Fargate Worker Mode

The repo now includes the first backend seam for moving scraper execution off the always-on EC2 host.

Relevant config fields in `control_panel.json`:
- `job_backend`
- `ecs_backend.region`
- `ecs_backend.cluster`
- `ecs_backend.task_definition`
- `ecs_backend.container_name`
- `ecs_backend.subnets`
- `ecs_backend.security_groups`
- `ecs_backend.assign_public_ip`
- `ecs_backend.artifact_bucket`
- `ecs_backend.artifact_prefix`

What is implemented now:
- control plane can be configured for `local_subprocess` or `ecs_fargate`
- job rows store backend, remote task id, remote status, and artifact prefix
- system page shows the configured execution backend
- system page reports ECS runtime readiness, including STS identity and task-definition lookup status
- ECS task launch and polling helpers exist for Fargate mode
- worker image can be built from `Dockerfile.worker`
- GitHub Actions can publish the worker image to ECR
- a starter ECS task definition template exists at `deploy/aws/ecs-task-definition.worker.json`

What still needs to be completed for full cutover:
- ECS cluster/task definition provisioning
- CloudWatch log stream wiring in job detail pages
- S3 artifact upload/download flow from worker tasks
- production switch from `local_subprocess` to `ecs_fargate`

### Worker image publishing

The repo now includes:
- `Dockerfile.worker`
- `.github/workflows/build-worker.yml`
- `deploy/aws/setup-ecs-worker.sh`
- `deploy/aws/ecs-task-definition.worker.json`

Expected GitHub Actions secrets for worker publishing:
- `AWS_ROLE_TO_ASSUME`
- `AWS_REGION`
- `ECR_REPOSITORY`

The workflow:
1. assumes an AWS role via OIDC
2. logs into ECR
3. creates the ECR repository if missing
4. builds the worker image
5. pushes both `:latest` and `:<git-sha>`

### AWS-side setup for workers

The first infrastructure helper is:
- `deploy/aws/setup-ecs-worker.sh`

It prepares:
- ECR repository
- ECS cluster
- CloudWatch log group
- task definition registration from the template

Required environment variables when running it:
- `AWS_ACCOUNT_ID`
- `AWS_REGION`
- `ECR_REPOSITORY`
- `ECS_CLUSTER`
- `EXECUTION_ROLE_ARN`
- `TASK_ROLE_ARN`
- optionally `TASK_FAMILY`, `CONTAINER_NAME`, `SECRETS_MANAGER_ID`

After those resources exist, the control plane can be pointed at them with:
- `job_backend: "ecs_fargate"`
- populated `ecs_backend` fields in `control_panel.production.json`

## Weekly Automation on macOS

Use the template:
- [launchd/com.yourbarmate.suppliers.weekly.plist.template](/Users/jakobJakob/Documents/New%20project%202/launchd/com.yourbarmate.suppliers.weekly.plist.template)

Setup steps:
1. Replace every `__PROJECT_ROOT__` placeholder with the absolute project path.
2. Copy the file into `~/Library/LaunchAgents/`.
3. Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.yourbarmate.suppliers.weekly.plist
```

4. Trigger a manual test run:

```bash
launchctl start com.yourbarmate.suppliers.weekly
```

5. Inspect:
- `logs/launchd-stdout.log`
- `logs/launchd-stderr.log`
- `logs/service.log`
- `logs/<supplier>/scrape.log`

The template currently schedules the job weekly on Monday at 03:30 local time.

## AWS Deployment

The repo now includes a single-EC2 deployment scaffold under [deploy/aws](/Users/jakobJakob/Documents/New%20project%202/deploy/aws).

Included artifacts:
- [deploy/aws/control_panel.production.json](/Users/jakobJakob/Documents/New%20project%202/deploy/aws/control_panel.production.json)
- [deploy/aws/yourbarmate-suppliers.service](/Users/jakobJakob/Documents/New%20project%202/deploy/aws/yourbarmate-suppliers.service)
- [deploy/aws/Caddyfile](/Users/jakobJakob/Documents/New%20project%202/deploy/aws/Caddyfile)
- [deploy/aws/yourbarmate-suppliers.env.example](/Users/jakobJakob/Documents/New%20project%202/deploy/aws/yourbarmate-suppliers.env.example)
- [deploy/aws/bootstrap-ec2.sh](/Users/jakobJakob/Documents/New%20project%202/deploy/aws/bootstrap-ec2.sh)
- [deploy/aws/backup.sh](/Users/jakobJakob/Documents/New%20project%202/deploy/aws/backup.sh)

Recommended runtime layout on EC2:
- app checkout: `/opt/yourbarmate-suppliers/app`
- virtualenv: `/opt/yourbarmate-suppliers/venv`
- persistent data: `/var/lib/yourbarmate-suppliers`
- logs: `/var/log/yourbarmate-suppliers`
- env file: `/etc/yourbarmate-suppliers.env`

Linux runtime path env vars:
- `SUPPLIER_OUTPUT_ROOT`
- `SCRAPER_STATE_ROOT`
- `SCRAPER_LOG_ROOT`
- `SCRAPER_CACHE_ROOT`
- `PLAYWRIGHT_BROWSERS_PATH`

High-level EC2 setup:
1. Launch an Ubuntu EC2 instance with persistent EBS storage.
2. Point a Route 53 record such as `suppliers.yourdomain.com` to the instance.
3. Clone the repo to `/opt/yourbarmate-suppliers/app`.
4. Run [bootstrap-ec2.sh](/Users/jakobJakob/Documents/New%20project%202/deploy/aws/bootstrap-ec2.sh).
5. Fill in `/etc/yourbarmate-suppliers.env` with real secrets and tokens.
6. Replace the placeholder domain in [Caddyfile](/Users/jakobJakob/Documents/New%20project%202/deploy/aws/Caddyfile).
7. Start `caddy` and `yourbarmate-suppliers` with `systemctl`.

Production notes:
- `control_panel.production.json` enables HTTPS-only session cookies.
- placeholder secrets are rejected when `enforce_non_placeholder_secrets` is enabled.
- the app binds to `127.0.0.1` and is intended to sit behind Caddy.
- APScheduler remains the production scheduler; `launchd` is local-only.

## Validation

Validation runs before sync and currently blocks sync on:
- CSV schema mismatch
- duplicate local `Item ID` values
- invalid enum values
- currency symbols left in `Price`

The cleaning pass also normalizes:
- `Item ID` to URL-safe format
- vessel and bundle types to canonical codes
- bundle fallback types
- `Price`, `VAT`, `Labels`, `GTIN`, and URLs

Validation warnings do not block sync, for example:
- parse failures that were still logged to the failure report
- fewer covered discovered URLs than expected
- missing images

Run summaries are written to:
- `output/<supplier>/<supplier>_run_summary.json`

## Tests

Run the test suite with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Current tests cover:
- deterministic product ID generation
- deterministic category ID generation
- YourBarMate payload mapping
- auth header usage
- remote pagination handling
- create / update / unchanged / inactivate sync behavior
- control panel auth helpers
- control panel queue overlap protection
- control panel supplier summaries and artifact allowlisting

## Known Limitations

- Only the Swissbox supplier adapter is implemented so far.
- The platform assumes YourBarMate accepts the documented product/category payloads exactly as used here.
- Product updates use `PATCH` with full managed fields; fields not modeled locally are intentionally not managed.
- Country is only synced when already available as a 2-letter code.
- Some scraped fields remain local-only because YourBarMate does not expose matching product fields.
